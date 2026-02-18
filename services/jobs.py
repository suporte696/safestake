from sqlalchemy.orm import Session

from services.notification_jobs import run_result_deadline_jobs


def run_scheduled_jobs(db: Session) -> dict:
    result_deadline = run_result_deadline_jobs(db)
    return {"result_deadline": result_deadline}
