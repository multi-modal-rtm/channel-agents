# arq chosen over Celery: tasks are plain async def, Redis is already a hard
# dependency (Upstash in prod), and there is no sync/async bridging overhead.
from arq.connections import RedisSettings
from arq.cron import cron

from app.config import settings
from app.workers.tasks.handle_incoming_message import handle_incoming_message
from app.workers.tasks.safety_job import run_safety_checks


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions = [handle_incoming_message]
    # Safety checks run every 5 minutes at :00, :05, :10, …, :55
    cron_jobs = [
        cron(run_safety_checks, minute={i * 5 for i in range(12)})
    ]
    max_jobs = 10
    job_timeout = 300  # seconds


# Run with: python -m arq app.workers.arq_app.WorkerSettings
