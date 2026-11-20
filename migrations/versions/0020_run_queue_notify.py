"""Run queue notifications (docs/architecture.md, Section 4.3).

A trigger on ``runs`` sends ``NOTIFY sama_runs`` when a run becomes ``queued`` and when an
active run ends. A worker that listens then claims the run at once and does not wait for its
next poll. Postgres sends the notification only when the transaction commits, and it sends one
notification for each transaction, so a burst of changes costs one message.

The trigger adds no table and no column, so the row level security does not change. The
function lives in ``sama_private``, which the Data API does not show.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL = "sama_runs"


def upgrade() -> None:
    op.execute(
        f"""
CREATE FUNCTION sama_private.notify_run_queue() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
  PERFORM pg_notify('{CHANNEL}', '');
  RETURN NULL;
END
$$
"""
    )
    # A new queued run, a run that is queued again, and an active run that ends. The end of an
    # active run can let the next run of its session or of its org start.
    op.execute(
        """
CREATE TRIGGER runs_notify_insert AFTER INSERT ON public.runs
FOR EACH ROW WHEN (NEW.status = 'queued')
EXECUTE FUNCTION sama_private.notify_run_queue()
"""
    )
    op.execute(
        """
CREATE TRIGGER runs_notify_update AFTER UPDATE OF status ON public.runs
FOR EACH ROW WHEN (
  NEW.status IS DISTINCT FROM OLD.status
  AND (NEW.status = 'queued' OR OLD.status IN ('running', 'paused'))
)
EXECUTE FUNCTION sama_private.notify_run_queue()
"""
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS runs_notify_update ON public.runs")
    op.execute("DROP TRIGGER IF EXISTS runs_notify_insert ON public.runs")
    op.execute("DROP FUNCTION IF EXISTS sama_private.notify_run_queue()")
