from datetime import datetime


def compute_event_status(event: dict, now_dt: datetime) -> dict:
    """Classify dose_event dict into status and ordering helpers."""
    try:
        sched_dt = datetime.fromisoformat(event.get("scheduled_time"))
    except Exception:
        sched_dt = now_dt

    if event.get("skipped"):
        status = "Omitida"
        bucket = 2
    elif event.get("taken"):
        status = "Tomada"
        bucket = 2
    elif sched_dt < now_dt:
        status = "Vencida"
        bucket = 0
    else:
        status = "Próxima"
        bucket = 1

    return {
        "status": status,
        "order_bucket": bucket,
        "sched_dt": sched_dt,
        "hour_label": sched_dt.strftime("%H:00"),
    }
