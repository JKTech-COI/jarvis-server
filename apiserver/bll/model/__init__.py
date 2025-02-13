from datetime import datetime
from typing import Callable, Tuple, Sequence, Dict, Optional

from mongoengine import Q

from apiserver.apierrors import errors
from apiserver.apimodels.models import ModelTaskPublishResponse
from apiserver.bll.task.utils import deleted_prefix
from apiserver.database.model import EntityVisibility
from apiserver.database.model.model import Model
from apiserver.database.model.task.task import Task, TaskStatus
from .metadata import Metadata


class ModelBLL:
    @classmethod
    def get_company_model_by_id(
        cls, company_id: str, model_id: str, only_fields=None
    ) -> Model:
        query = dict(company=company_id, id=model_id)
        qs = Model.objects(**query)
        if only_fields:
            qs = qs.only(*only_fields)
        model = qs.first()
        if not model:
            raise errors.bad_request.InvalidModelId(**query)
        return model

    @staticmethod
    def assert_exists(
        company_id,
        model_ids,
        only=None,
        allow_public=False,
        return_models=True,
    ) -> Optional[Sequence[Model]]:
        model_ids = [model_ids] if isinstance(model_ids, str) else model_ids
        ids = set(model_ids)
        query = Q(id__in=ids)

        q = Model.get_many(
            company=company_id,
            query=query,
            allow_public=allow_public,
            return_dicts=False,
        )
        if only:
            q = q.only(*only)

        if q.count() != len(ids):
            raise errors.bad_request.InvalidModelId(ids=model_ids)

        if return_models:
            return list(q)

    @classmethod
    def publish_model(
        cls,
        model_id: str,
        company_id: str,
        user_id: str,
        force_publish_task: bool = False,
        publish_task_func: Callable[[str, str, str, bool], dict] = None,
    ) -> Tuple[int, ModelTaskPublishResponse]:
        model = cls.get_company_model_by_id(company_id=company_id, model_id=model_id)
        if model.ready:
            raise errors.bad_request.ModelIsReady(company=company_id, model=model_id)

        published_task = None
        if model.task and publish_task_func:
            task = (
                Task.objects(id=model.task, company=company_id)
                .only("id", "status")
                .first()
            )
            if task and task.status != TaskStatus.published:
                task_publish_res = publish_task_func(
                    model.task, company_id, user_id, force_publish_task
                )
                published_task = ModelTaskPublishResponse(
                    id=model.task, data=task_publish_res
                )

        updated = model.update(upsert=False, ready=True, last_update=datetime.utcnow())
        return updated, published_task

    @classmethod
    def delete_model(
        cls, model_id: str, company_id: str, force: bool
    ) -> Tuple[int, Model]:
        model = cls.get_company_model_by_id(
            company_id=company_id,
            model_id=model_id,
            only_fields=("id", "task", "project", "uri"),
        )
        deleted_model_id = f"{deleted_prefix}{model_id}"

        using_tasks = Task.objects(models__input__model=model_id).only("id")
        if using_tasks:
            if not force:
                raise errors.bad_request.ModelInUse(
                    "as execution model, use force=True to delete",
                    num_tasks=len(using_tasks),
                )
            # update deleted model id in using tasks
            Task._get_collection().update_many(
                filter={"_id": {"$in": [t.id for t in using_tasks]}},
                update={"$set": {"models.input.$[elem].model": deleted_model_id}},
                array_filters=[{"elem.model": model_id}],
                upsert=False,
            )

        if model.task:
            task = Task.objects(id=model.task).first()
            if task and task.status == TaskStatus.published:
                if not force:
                    raise errors.bad_request.ModelCreatingTaskExists(
                        "and published, use force=True to delete", task=model.task
                    )
                if task.models.output and model_id in task.models.output:
                    now = datetime.utcnow()
                    Task._get_collection().update_one(
                        filter={"_id": model.task, "models.output.model": model_id},
                        update={
                            "$set": {
                                "models.output.$[elem].model": deleted_model_id,
                                "output.error": f"model deleted on {now.isoformat()}",
                            },
                            "last_change": now,
                        },
                        array_filters=[{"elem.model": model_id}],
                        upsert=False,
                    )

        del_count = Model.objects(id=model_id, company=company_id).delete()
        return del_count, model

    @classmethod
    def archive_model(cls, model_id: str, company_id: str):
        cls.get_company_model_by_id(
            company_id=company_id, model_id=model_id, only_fields=("id",)
        )
        archived = Model.objects(company=company_id, id=model_id).update(
            add_to_set__system_tags=EntityVisibility.archived.value,
            last_update=datetime.utcnow(),
        )

        return archived

    @classmethod
    def unarchive_model(cls, model_id: str, company_id: str):
        cls.get_company_model_by_id(
            company_id=company_id, model_id=model_id, only_fields=("id",)
        )
        unarchived = Model.objects(company=company_id, id=model_id).update(
            pull__system_tags=EntityVisibility.archived.value,
            last_update=datetime.utcnow(),
        )

        return unarchived

    @classmethod
    def get_model_stats(
        cls, company: str, model_ids: Sequence[str],
    ) -> Dict[str, dict]:
        if not model_ids:
            return {}

        result = Model.aggregate(
            [
                {
                    "$match": {
                        "company": {"$in": [None, "", company]},
                        "_id": {"$in": model_ids},
                    }
                },
                {
                    "$addFields": {
                        "labels_count": {"$size": {"$objectToArray": "$labels"}}
                    }
                },
                {
                    "$project": {"labels_count": 1},
                },
            ]
        )
        return {
            r.pop("_id"): r
            for r in result
        }
