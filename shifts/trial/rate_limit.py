"""公開Trialの生成API呼び出しを保護するレート制限。"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from shifts.models import TrialGenerationQuota

TRIAL_GENERATION_STALE_SECONDS = 360

TRIAL_SESSION_DAILY_LIMIT = 5
TRIAL_IP_DAILY_LIMIT = 15
TRIAL_GLOBAL_DAILY_LIMIT = 100

TRIAL_VISITOR_SESSION_KEY = "trial_visitor_id"
GLOBAL_SCOPE_KEY = "global"
UNAVAILABLE_IP_KEY = "unavailable-client-ip"

SESSION_DAILY_LIMIT_MESSAGE = (
    "本日の体験版のシフト生成上限（5回）に達しました。"
    "明日以降に再度お試しください。"
)
IP_DAILY_LIMIT_MESSAGE = (
    "このネットワークからの本日の体験版利用上限に達しました。"
    "明日以降に再度お試しください。"
)
GLOBAL_DAILY_LIMIT_MESSAGE = (
    "本日の体験版の利用上限に達しました。"
    "明日以降に再度お試しください。"
)
GENERATION_IN_PROGRESS_MESSAGE = "現在シフトを生成中です。生成完了後に再度お試しください。"


@dataclass(frozen=True)
class TrialRateLimitResult:
    allowed: bool
    message: str = ""
    retry_after: int | None = None
    generation_started_at: datetime | None = None


def get_trial_visitor_id(session) -> str:
    """Trial専用の匿名UUIDをSessionに1回だけ作成する。"""

    visitor_id = session.get(TRIAL_VISITOR_SESSION_KEY)
    if isinstance(visitor_id, str):
        try:
            return str(uuid.UUID(visitor_id))
        except ValueError:
            pass

    visitor_id = str(uuid.uuid4())
    session[TRIAL_VISITOR_SESSION_KEY] = visitor_id
    return visitor_id


def get_client_ip(request) -> str | None:
    """明示的に信頼したプロキシ環境だけでX-Forwarded-Forを利用する。"""

    if getattr(settings, "TRIAL_TRUST_X_FORWARDED_FOR", False):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        candidate = forwarded_for.split(",", 1)[0].strip()
    else:
        candidate = request.META.get("REMOTE_ADDR", "").strip()

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def get_client_ip_hash(request) -> str:
    """生IPを永続化せず、SECRET_KEYを用いたHMACだけを識別子にする。"""

    client_ip = get_client_ip(request) or UNAVAILABLE_IP_KEY
    return hmac.new(
        settings.SECRET_KEY.encode(),
        client_ip.encode(),
        hashlib.sha256,
    ).hexdigest()


def consume_trial_generation_quota(request, *, now=None) -> TrialRateLimitResult:
    """日次上限を確認し、IPを生成中としてからAPI呼び出しを許可する。"""

    now = now or timezone.now()
    target_date = timezone.localdate(now)
    visitor_id = get_trial_visitor_id(request.session)
    ip_hash = get_client_ip_hash(request)

    with transaction.atomic():
        # 全体 → IP → Session の順でロックし、同じ順序を全リクエストで保つ。
        global_quota = _get_locked_quota(
            TrialGenerationQuota.ScopeType.GLOBAL,
            GLOBAL_SCOPE_KEY,
            target_date,
        )
        ip_quota = _get_locked_quota(
            TrialGenerationQuota.ScopeType.IP,
            ip_hash,
            target_date,
        )
        session_quota = _get_locked_quota(
            TrialGenerationQuota.ScopeType.SESSION,
            visitor_id,
            target_date,
        )

        stale_before = now - timedelta(seconds=TRIAL_GENERATION_STALE_SECONDS)
        if (
            ip_quota.generation_started_at is not None
            and ip_quota.generation_started_at > stale_before
        ):
            return TrialRateLimitResult(
                allowed=False,
                message=GENERATION_IN_PROGRESS_MESSAGE,
            )

        if session_quota.generation_count >= TRIAL_SESSION_DAILY_LIMIT:
            return TrialRateLimitResult(allowed=False, message=SESSION_DAILY_LIMIT_MESSAGE)
        if ip_quota.generation_count >= TRIAL_IP_DAILY_LIMIT:
            return TrialRateLimitResult(allowed=False, message=IP_DAILY_LIMIT_MESSAGE)
        if global_quota.generation_count >= TRIAL_GLOBAL_DAILY_LIMIT:
            return TrialRateLimitResult(allowed=False, message=GLOBAL_DAILY_LIMIT_MESSAGE)

        for quota in (global_quota, ip_quota, session_quota):
            quota.generation_count += 1
            quota.last_generation_at = now
            quota.save(update_fields=["generation_count", "last_generation_at", "updated_at"])

        ip_quota.generation_started_at = now
        ip_quota.save(update_fields=["generation_started_at", "updated_at"])

    return TrialRateLimitResult(allowed=True, generation_started_at=now)


def release_trial_generation_lock(request, generation_started_at) -> None:
    """自分が設定した生成中状態だけを解除する。"""

    TrialGenerationQuota.objects.filter(
        scope_type=TrialGenerationQuota.ScopeType.IP,
        scope_key=get_client_ip_hash(request),
        generation_started_at=generation_started_at,
    ).update(generation_started_at=None)


def _get_locked_quota(scope_type: str, scope_key: str, target_date) -> TrialGenerationQuota:
    """UniqueConstraintと行ロックで、同日の上限突破を防ぐ。"""

    quota, _ = TrialGenerationQuota.objects.get_or_create(
        scope_type=scope_type,
        scope_key=scope_key,
        date=target_date,
    )
    return TrialGenerationQuota.objects.select_for_update().get(pk=quota.pk)
