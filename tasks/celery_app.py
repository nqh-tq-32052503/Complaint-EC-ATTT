from celery import Celery
import config.celery_config as celery_config

celery_app = Celery("complaint_ec_pipeline")
celery_app.config_from_object(celery_config)

# Import tasks so they are registered when celery worker starts
import tasks.pipeline_tasks  # noqa: E402, F401
