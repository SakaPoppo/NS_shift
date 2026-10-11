"""行動イベントを保存確定後に記録する。記録失敗は主要処理へ伝播させない。"""

import logging
from uuid import UUID

from django.db import transaction

from shifts.trial.rate_limit import TRIAL_VISITOR_SESSION_KEY

from .models import UserEvent

logger = logging.getLogger(__name__)


def track_event(event_type, request=None, user=None):
    try:
        event_type = UserEvent.EventType(event_type)
        if user is None and request is not None:
            user = getattr(request, "user", None)
        user_id = user.pk if user is not None and user.is_authenticated else None
        session = getattr(request, "session", {})
        visitor_id = session.get(TRIAL_VISITOR_SESSION_KEY)
        if visitor_id is not None:
            visitor_id = UUID(str(visitor_id))

        def insert_event():
            try:
                # 呼び出し元のトランザクションをDBエラーで壊さない。
                with transaction.atomic():
                    UserEvent.objects.create(
                        event_type=event_type,
                        user_id=user_id,
                        visitor_id=visitor_id,
                    )
            except Exception:
                logger.exception("UserEvent insert failed (event_type=%s)", event_type)

        transaction.on_commit(insert_event)
    except Exception:
        logger.exception("UserEvent scheduling failed")
