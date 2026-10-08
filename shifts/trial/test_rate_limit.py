from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier

from django.contrib.sessions.middleware import SessionMiddleware
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.test import (
    RequestFactory,
    TestCase,
    TransactionTestCase,
    override_settings,
    skipUnlessDBFeature,
)
from django.utils import timezone

from shifts.models import TrialGenerationQuota

from .rate_limit import (
    GENERATION_IN_PROGRESS_MESSAGE,
    GLOBAL_DAILY_LIMIT_MESSAGE,
    IP_DAILY_LIMIT_MESSAGE,
    SESSION_DAILY_LIMIT_MESSAGE,
    TRIAL_GENERATION_STALE_SECONDS,
    TRIAL_GLOBAL_DAILY_LIMIT,
    TRIAL_IP_DAILY_LIMIT,
    TRIAL_SESSION_DAILY_LIMIT,
    TRIAL_VISITOR_SESSION_KEY,
    consume_trial_generation_quota,
    get_client_ip,
    get_client_ip_hash,
    release_trial_generation_lock,
)


class TrialGenerationRateLimitTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.now = timezone.make_aware(datetime(2026, 10, 8, 12))

    def make_request(self, ip="198.51.100.10", session=None):
        request = self.factory.post("/shifts/trial/", {"action": "generate"})
        if session is None:
            SessionMiddleware(lambda _: None).process_request(request)
            request.session.save()
        else:
            request.session = session
        request.META["REMOTE_ADDR"] = ip
        return request

    def release(self, request, result):
        release_trial_generation_lock(request, result.generation_started_at)

    def test_first_generation_sets_ip_generation_started_at(self):
        request = self.make_request()

        result = consume_trial_generation_quota(request, now=self.now)

        self.assertTrue(result.allowed)
        self.assertEqual(TrialGenerationQuota.objects.count(), 3)
        self.assertEqual(
            TrialGenerationQuota.objects.get(scope_type="ip").generation_started_at,
            self.now,
        )

    def test_generation_in_progress_rejects_same_ip(self):
        first_request = self.make_request()
        consume_trial_generation_quota(first_request, now=self.now)

        result = consume_trial_generation_quota(
            self.make_request(session=first_request.session),
            now=self.now,
        )

        self.assertFalse(result.allowed)
        self.assertEqual(result.message, GENERATION_IN_PROGRESS_MESSAGE)
        self.assertIsNone(result.retry_after)

    def test_generation_in_progress_applies_to_a_different_session(self):
        first_request = self.make_request()
        consume_trial_generation_quota(first_request, now=self.now)

        second_request = self.make_request()
        result = consume_trial_generation_quota(second_request, now=self.now)

        self.assertFalse(result.allowed)
        self.assertEqual(result.message, GENERATION_IN_PROGRESS_MESSAGE)

    def test_session_daily_limit_allows_five_then_rejects_sixth(self):
        request = self.make_request()
        for _ in range(TRIAL_SESSION_DAILY_LIMIT):
            result = consume_trial_generation_quota(request, now=self.now)
            self.assertTrue(result.allowed)
            self.release(request, result)

        result = consume_trial_generation_quota(request, now=self.now)

        self.assertFalse(result.allowed)
        self.assertEqual(result.message, SESSION_DAILY_LIMIT_MESSAGE)
        self.assertIsNone(result.retry_after)

    def test_ip_daily_limit_survives_session_changes(self):
        for _ in range(TRIAL_IP_DAILY_LIMIT):
            request = self.make_request()
            result = consume_trial_generation_quota(request, now=self.now)
            self.assertTrue(result.allowed)
            self.release(request, result)

        result = consume_trial_generation_quota(self.make_request(), now=self.now)

        self.assertFalse(result.allowed)
        self.assertEqual(result.message, IP_DAILY_LIMIT_MESSAGE)
        self.assertIsNone(result.retry_after)

    def test_global_daily_limit_survives_ip_changes(self):
        TrialGenerationQuota.objects.create(
            scope_type=TrialGenerationQuota.ScopeType.GLOBAL,
            scope_key="global",
            date=timezone.localdate(self.now),
            generation_count=TRIAL_GLOBAL_DAILY_LIMIT,
        )

        result = consume_trial_generation_quota(self.make_request(), now=self.now)

        self.assertFalse(result.allowed)
        self.assertEqual(result.message, GLOBAL_DAILY_LIMIT_MESSAGE)
        self.assertIsNone(result.retry_after)

    def test_daily_quotas_reset_on_the_next_local_date(self):
        request = self.make_request()

        first_result = consume_trial_generation_quota(request, now=self.now)
        self.assertTrue(first_result.allowed)
        self.release(request, first_result)
        second_result = consume_trial_generation_quota(
            request,
            now=self.now + timedelta(days=1),
        )
        self.assertTrue(second_result.allowed)
        self.assertEqual(TrialGenerationQuota.objects.count(), 6)
        for quota in TrialGenerationQuota.objects.all():
            self.assertEqual(quota.generation_count, 1)

    def test_different_ip_can_generate_while_first_ip_is_generating(self):
        request = self.make_request()
        consume_trial_generation_quota(request, now=self.now)
        result = consume_trial_generation_quota(
            self.make_request(ip="198.51.100.11"),
            now=self.now,
        )
        self.assertTrue(result.allowed)

    def test_release_clears_generation_lock(self):
        request = self.make_request()
        result = consume_trial_generation_quota(request, now=self.now)
        self.release(request, result)

        ip_quota = TrialGenerationQuota.objects.get(scope_type="ip")
        self.assertIsNone(ip_quota.generation_started_at)

    def test_stale_generation_lock_is_replaced_after_six_minutes(self):
        request = self.make_request()
        TrialGenerationQuota.objects.create(
            scope_type=TrialGenerationQuota.ScopeType.IP,
            scope_key=get_client_ip_hash(request),
            date=timezone.localdate(self.now),
            generation_started_at=self.now
            - timedelta(seconds=TRIAL_GENERATION_STALE_SECONDS),
        )

        result = consume_trial_generation_quota(request, now=self.now)

        self.assertTrue(result.allowed)
        self.assertEqual(
            TrialGenerationQuota.objects.get(scope_type="ip").generation_started_at,
            self.now,
        )

    def test_quota_scope_and_date_must_be_unique(self):
        request = self.make_request()
        consume_trial_generation_quota(request, now=self.now)
        quota = TrialGenerationQuota.objects.get(scope_type="ip")
        with self.assertRaises(IntegrityError), transaction.atomic():
            TrialGenerationQuota.objects.create(
                scope_type=quota.scope_type,
                scope_key=quota.scope_key,
                date=quota.date,
            )
        self.assertEqual(TrialGenerationQuota.objects.count(), 3)

    def test_only_hmac_hash_is_persisted_for_client_ip(self):
        raw_ip = "203.0.113.25"
        request = self.make_request(ip=raw_ip)
        consume_trial_generation_quota(request, now=self.now)

        self.assertFalse(
            TrialGenerationQuota.objects.filter(scope_key=raw_ip).exists()
        )
        ip_quota = TrialGenerationQuota.objects.get(scope_type="ip")
        self.assertEqual(ip_quota.scope_key, get_client_ip_hash(request))
        self.assertEqual(len(ip_quota.scope_key), 64)

    @override_settings(TRIAL_TRUST_X_FORWARDED_FOR=True)
    def test_render_proxy_uses_leftmost_forwarded_ip(self):
        request = self.make_request(ip="10.0.0.10")
        request.META["HTTP_X_FORWARDED_FOR"] = "2001:db8::10, 10.0.0.10"

        self.assertEqual(get_client_ip(request), "2001:db8::10")

    @override_settings(TRIAL_TRUST_X_FORWARDED_FOR=False)
    def test_untrusted_proxy_uses_remote_addr(self):
        request = self.make_request(ip="198.51.100.10")
        request.META["HTTP_X_FORWARDED_FOR"] = "203.0.113.25"
        self.assertEqual(get_client_ip(request), "198.51.100.10")


class TrialQuotaConcurrencyTests(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_requests_cannot_exceed_ip_daily_limit(self):
        now = timezone.make_aware(datetime(2026, 10, 8, 12))
        requests = []
        for visitor_id in (
            "00000000-0000-0000-0000-000000000001",
            "00000000-0000-0000-0000-000000000002",
        ):
            request = RequestFactory().post("/shifts/trial/")
            request.session = {TRIAL_VISITOR_SESSION_KEY: visitor_id}
            request.META["REMOTE_ADDR"] = "198.51.100.10"
            requests.append(request)
        TrialGenerationQuota.objects.create(
            scope_type="ip",
            scope_key=get_client_ip_hash(requests[0]),
            date=timezone.localdate(now),
            generation_count=14,
        )
        barrier = Barrier(2)

        def consume(request):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return consume_trial_generation_quota(request, now=now).allowed
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, requests))

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(
            TrialGenerationQuota.objects.get(scope_type="ip").generation_count, 15,
        )
        self.assertEqual(
            TrialGenerationQuota.objects.get(scope_type="global").generation_count, 1,
        )
