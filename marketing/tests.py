"""
Tests for the Marketing domain: script_key rejection without leaking why,
server-side rate limiting (per-IP, per-script_key, and the cross-IP
aggregate gap), payload validation, tenant isolation, and OAuth token
encryption at rest.

Throttle-threshold tests use `patch.dict` against
`rest_framework.throttling.SimpleRateThrottle.THROTTLE_RATES` (the actual
dict every throttle instance reads `self.scope` out of at request time) to
temporarily shrink a rate for the duration of one test. This proves the
*mechanism* works without literally sending hundreds-to-thousands of
requests to exercise the real production numbers (300/minute,
6000/minute, etc, documented in config/settings.py and README) — those
numbers are a capacity/abuse-budget choice, not something that needs a
slow test to "prove."
"""

import uuid
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework.throttling import SimpleRateThrottle

from authentication.models import User
from core.models import Business, BusinessMembership

from . import services
from .models import FormSubmission, GoogleAdsCampaign, Lead, PageView, TrackingEvent, TrackingScript, WebsiteVisitor

TRACK_URL = "/api/public/track/"
FORM_SUBMIT_URL = "/api/public/forms/submit/"


def script_list_url(business_id):
    return f"/api/businesses/{business_id}/tracking-scripts/"


def script_regenerate_url(business_id, pk):
    return f"/api/businesses/{business_id}/tracking-scripts/{pk}/regenerate-key/"


def visitor_list_url(business_id):
    return f"/api/businesses/{business_id}/website-visitors/"


def page_view_list_url(business_id):
    return f"/api/businesses/{business_id}/page-views/"


def lead_list_url(business_id):
    return f"/api/businesses/{business_id}/leads/"


def form_submission_list_url(business_id):
    return f"/api/businesses/{business_id}/form-submissions/"


def campaign_list_url(business_id):
    return f"/api/businesses/{business_id}/google-ads-campaigns/"


def campaign_detail_url(business_id, pk):
    return f"/api/businesses/{business_id}/google-ads-campaigns/{pk}/"


def _patched_rate(scope, rate):
    return patch.dict(SimpleRateThrottle.THROTTLE_RATES, {scope: rate})


def _generate_fernet_key():
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


class ScriptKeyRejectionTests(TestCase):
    """
    script_key identifies a business, not an authorized caller, and is
    visible in client-side JS — so a nonexistent, inactive, or
    well-formed-but-wrong key must be indistinguishable to the caller.
    """

    def setUp(self):
        owner = User.objects.create_user(email="owner_key@example.com")
        self.business = Business.objects.create(name="Key Biz", owner=owner)
        self.active_script = TrackingScript.objects.create(
            business=self.business, script_key=services.generate_script_key()
        )
        self.inactive_script = TrackingScript.objects.create(
            business=self.business, script_key=services.generate_script_key(), is_active=False
        )
        self.client = APIClient()

    def _track_payload(self, script_key):
        return {"script_key": script_key, "kind": "pageview", "url": "https://example.com/"}

    def test_rejections_are_identical_across_failure_modes(self):
        nonexistent_response = self.client.post(TRACK_URL, self._track_payload("totally-nonexistent-key"))
        inactive_response = self.client.post(TRACK_URL, self._track_payload(self.inactive_script.script_key))
        garbage_response = self.client.post(TRACK_URL, self._track_payload(""))

        for response in (nonexistent_response, inactive_response):
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
            self.assertEqual(response.data, {"detail": "Invalid request."})

        # Empty script_key fails required-field validation rather than
        # resolution — still a 400, but via the normal field-error path
        # (shape, not secrecy). Confirm it's not silently treated as valid.
        self.assertEqual(garbage_response.status_code, status.HTTP_400_BAD_REQUEST)

        self.assertEqual(nonexistent_response.data, inactive_response.data)
        self.assertEqual(nonexistent_response.status_code, inactive_response.status_code)

    def test_valid_active_key_is_accepted(self):
        response = self.client.post(TRACK_URL, self._track_payload(self.active_script.script_key))
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertNotEqual(response.data, {"detail": "Invalid request."})


class PayloadValidationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_payload@example.com")
        self.business = Business.objects.create(name="Payload Biz", owner=owner)
        self.script = TrackingScript.objects.create(business=self.business, script_key=services.generate_script_key())
        self.client = APIClient()

    def test_oversized_metadata_rejected(self):
        response = self.client.post(
            TRACK_URL,
            {
                "script_key": self.script.script_key,
                "kind": "event",
                "event_type": "custom",
                "metadata": {"blob": "x" * 5000},
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(TrackingEvent.objects.count(), 0)

    def test_non_dict_metadata_rejected(self):
        response = self.client.post(
            TRACK_URL,
            {"script_key": self.script.script_key, "kind": "event", "event_type": "custom", "metadata": "not-a-dict"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_event_type_rejected(self):
        response = self.client.post(
            TRACK_URL,
            {"script_key": self.script.script_key, "kind": "event", "event_type": "totally_made_up"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pageview_without_url_rejected(self):
        response = self.client.post(TRACK_URL, {"script_key": self.script.script_key, "kind": "pageview"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_oversized_form_data_rejected(self):
        response = self.client.post(
            FORM_SUBMIT_URL,
            {"script_key": self.script.script_key, "form_data": {"blob": "x" * 10000}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(FormSubmission.objects.count(), 0)

    def test_valid_event_accepted(self):
        response = self.client.post(
            TRACK_URL,
            {"script_key": self.script.script_key, "kind": "event", "event_type": "click", "metadata": {"x": 1}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(TrackingEvent.objects.count(), 1)


class VisitorIdentityTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_visitor@example.com")
        self.business_a = Business.objects.create(name="Visitor Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Visitor Biz B", owner=owner)
        self.script_a = TrackingScript.objects.create(
            business=self.business_a, script_key=services.generate_script_key()
        )
        self.client = APIClient()

    def _track(self, **cookies):
        for name, value in cookies.items():
            self.client.cookies[name] = value
        return self.client.post(
            TRACK_URL, {"script_key": self.script_a.script_key, "kind": "pageview", "url": "https://example.com/a"}
        )

    def test_first_visit_creates_a_visitor_and_sets_a_cookie(self):
        response = self._track()
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn(services.VISITOR_COOKIE_NAME, response.cookies)
        self.assertEqual(WebsiteVisitor.objects.filter(business=self.business_a).count(), 1)

    def test_returning_visitor_with_valid_cookie_is_reused_not_duplicated(self):
        first = self._track()
        cookie_value = first.cookies[services.VISITOR_COOKIE_NAME].value
        self._track(**{services.VISITOR_COOKIE_NAME: cookie_value})
        self.assertEqual(WebsiteVisitor.objects.filter(business=self.business_a).count(), 1)
        self.assertEqual(PageView.objects.filter(visitor__business=self.business_a).count(), 2)

    def test_cookie_belonging_to_a_different_business_is_not_adopted(self):
        other_visitor = WebsiteVisitor.objects.create(business=self.business_b)
        self._track(**{services.VISITOR_COOKIE_NAME: str(other_visitor.id)})
        # A brand-new visitor was created for business_a — the
        # business_b cookie value was never trusted as authoritative.
        self.assertEqual(WebsiteVisitor.objects.filter(business=self.business_a).count(), 1)
        new_visitor = WebsiteVisitor.objects.get(business=self.business_a)
        self.assertNotEqual(new_visitor.id, other_visitor.id)

    def test_garbage_cookie_value_does_not_crash_and_creates_a_new_visitor(self):
        response = self._track(**{services.VISITOR_COOKIE_NAME: "not-a-uuid-at-all"})
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(WebsiteVisitor.objects.filter(business=self.business_a).count(), 1)


class HighFrequencyAbuseHeuristicTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_abuse@example.com")
        self.business = Business.objects.create(name="Abuse Biz", owner=owner)
        self.visitor = WebsiteVisitor.objects.create(business=self.business)

    def test_visitor_flagged_after_threshold_events_in_window(self):
        for _ in range(services.HIGH_FREQUENCY_THRESHOLD - 1):
            services.record_event(self.visitor, "click", {})
        self.visitor.refresh_from_db()
        self.assertFalse(self.visitor.is_suspicious)

        services.record_event(self.visitor, "click", {})
        self.visitor.refresh_from_db()
        self.assertTrue(self.visitor.is_suspicious)
        self.assertIsNotNone(self.visitor.flagged_at)

    def test_low_frequency_traffic_is_never_flagged(self):
        services.record_pageview(self.visitor, "https://example.com/")
        services.record_event(self.visitor, "click", {})
        self.visitor.refresh_from_db()
        self.assertFalse(self.visitor.is_suspicious)

    def test_flagging_does_not_block_the_request_it_just_records(self):
        old_window_event_time = timezone.now() - timedelta(minutes=5)
        for _ in range(services.HIGH_FREQUENCY_THRESHOLD):
            event = services.record_event(self.visitor, "click", {})
            TrackingEvent.objects.filter(pk=event.pk).update(timestamp=old_window_event_time)
        self.visitor.refresh_from_db()
        # All of those were backdated outside the window before the flag
        # check ran on the *next* call — i.e. old high-frequency bursts
        # don't retroactively/perpetually flag a visitor.
        services.record_event(self.visitor, "click", {})
        self.visitor.refresh_from_db()
        self.assertFalse(self.visitor.is_suspicious)


class FormSubmissionLeadLinkingTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_form@example.com")
        self.business = Business.objects.create(name="Form Biz", owner=owner)
        self.script = TrackingScript.objects.create(business=self.business, script_key=services.generate_script_key())
        self.client = APIClient()

    def test_submission_with_email_creates_a_lead(self):
        response = self.client.post(
            FORM_SUBMIT_URL,
            {"script_key": self.script.script_key, "form_data": {"name": "Alice", "email": "alice@example.com"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        lead = Lead.objects.get(business=self.business, email="alice@example.com")
        self.assertEqual(lead.name, "Alice")
        self.assertEqual(FormSubmission.objects.get().lead, lead)

    def test_repeat_submissions_from_same_email_reuse_one_lead(self):
        payload = {"script_key": self.script.script_key, "form_data": {"name": "Bob", "email": "bob@example.com"}}
        self.client.post(FORM_SUBMIT_URL, payload, format="json")
        self.client.post(FORM_SUBMIT_URL, payload, format="json")
        self.assertEqual(Lead.objects.filter(business=self.business, email="bob@example.com").count(), 1)
        self.assertEqual(FormSubmission.objects.count(), 2)

    def test_submission_without_email_creates_no_lead(self):
        response = self.client.post(
            FORM_SUBMIT_URL,
            {"script_key": self.script.script_key, "form_data": {"message": "no contact info"}},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertIsNone(FormSubmission.objects.get().lead)
        self.assertEqual(Lead.objects.count(), 0)

    def test_ip_address_stored_but_never_returned_by_the_staff_api(self):
        self.client.post(
            FORM_SUBMIT_URL,
            {"script_key": self.script.script_key, "form_data": {"email": "carol@example.com"}},
            format="json",
            REMOTE_ADDR="203.0.113.5",
        )
        submission = FormSubmission.objects.get()
        self.assertEqual(submission.ip_address, "203.0.113.5")

        staff_user = User.objects.create_user(email="staff_form@example.com")
        BusinessMembership.objects.create(business=self.business, user=staff_user, role=BusinessMembership.Role.STAFF)
        staff_client = APIClient()
        staff_client.force_authenticate(user=staff_user)
        response = staff_client.get(form_submission_list_url(self.business.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("ip_address", response.data[0])


class RateLimitingTests(TestCase):
    """
    See module docstring: THROTTLE_RATES is patched down per-test to make
    the threshold reachable quickly; the production numbers themselves
    live in config/settings.py and README "Marketing domain".
    """

    def setUp(self):
        from django.core.cache import cache

        cache.clear()
        owner = User.objects.create_user(email="owner_throttle@example.com")
        self.business = Business.objects.create(name="Throttle Biz", owner=owner)
        self.script = TrackingScript.objects.create(business=self.business, script_key=services.generate_script_key())
        self.client = APIClient()

    def _track(self, **extra):
        return self.client.post(
            TRACK_URL,
            {"script_key": self.script.script_key, "kind": "pageview", "url": "https://example.com/"},
            **extra,
        )

    def test_per_ip_throttle_rejects_the_nth_request_within_the_window(self):
        with _patched_rate("track_event_ip", "3/minute"):
            for _ in range(3):
                self.assertEqual(self._track().status_code, status.HTTP_202_ACCEPTED)
            self.assertEqual(self._track().status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_per_script_key_throttle_rejects_the_nth_request_regardless_of_ip(self):
        with _patched_rate("track_event_script_key", "3/minute"):
            for i in range(3):
                response = self._track(REMOTE_ADDR=f"10.1.0.{i}")
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            # A brand-new IP, never seen before — still rejected, because
            # the cap is on the script_key, not any one caller.
            response = self._track(REMOTE_ADDR="10.1.0.99")
            self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_distributed_burst_across_many_ips_still_hits_the_script_key_cap(self):
        # The same gap checked for Reservations' confirmation-code lookup:
        # each of these IPs individually makes only one request — nowhere
        # near the (real, generous) per-IP cap — but the aggregate,
        # IP-independent script_key cap still catches the combined burst.
        with _patched_rate("track_event_script_key", "10/minute"):
            for i in range(10):
                response = self._track(REMOTE_ADDR=f"10.2.0.{i}")
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            response = self._track(REMOTE_ADDR="10.2.0.250")
            self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_form_submit_per_ip_throttle_at_the_real_configured_rate(self):
        # form_submit_ip is already tight enough (10/minute) to exercise directly.
        for _ in range(10):
            response = self.client.post(
                FORM_SUBMIT_URL, {"script_key": self.script.script_key, "form_data": {}}, format="json"
            )
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        response = self.client.post(
            FORM_SUBMIT_URL, {"script_key": self.script.script_key, "form_data": {}}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)

    def test_form_submit_script_key_throttle_independent_of_ip(self):
        with _patched_rate("form_submit_script_key", "3/minute"):
            for i in range(3):
                response = self.client.post(
                    FORM_SUBMIT_URL,
                    {"script_key": self.script.script_key, "form_data": {}},
                    format="json",
                    REMOTE_ADDR=f"10.3.0.{i}",
                )
                self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            response = self.client.post(
                FORM_SUBMIT_URL,
                {"script_key": self.script.script_key, "form_data": {}},
                format="json",
                REMOTE_ADDR="10.3.0.250",
            )
            self.assertEqual(response.status_code, status.HTTP_429_TOO_MANY_REQUESTS)


class TrackingScriptManagementTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_script_mgmt@example.com")
        self.business = Business.objects.create(name="Script Mgmt Biz", owner=owner)
        self.staff_user = User.objects.create_user(email="staff_script_mgmt@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.script = TrackingScript.objects.create(business=self.business, script_key=services.generate_script_key())
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)

    def test_create_generates_a_server_side_key_ignoring_any_client_supplied_value(self):
        response = self.client.post(
            script_list_url(self.business.id), {"script_key": "attacker-chosen-key", "is_active": True}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        created = TrackingScript.objects.get(id=response.data["id"])
        self.assertNotEqual(created.script_key, "attacker-chosen-key")

    def test_regenerate_key_replaces_the_value_and_invalidates_the_old_one(self):
        old_key = self.script.script_key
        response = self.client.post(script_regenerate_url(self.business.id, self.script.id))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.script.refresh_from_db()
        self.assertNotEqual(self.script.script_key, old_key)
        self.assertIsNone(services.resolve_script_key(old_key))
        self.assertIsNotNone(services.resolve_script_key(self.script.script_key))

    def test_deactivating_a_script_makes_its_key_stop_resolving(self):
        response = self.client.patch(f"{script_list_url(self.business.id)}{self.script.id}/", {"is_active": False})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(services.resolve_script_key(self.script.script_key))
        # The row (and its key) still exist — "revoked," not destroyed.
        self.assertTrue(TrackingScript.objects.filter(id=self.script.id, script_key=self.script.script_key).exists())


class GoogleAdsCampaignEncryptionTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_oauth@example.com")
        self.business = Business.objects.create(name="OAuth Biz", owner=owner)
        self.staff_user = User.objects.create_user(email="staff_oauth@example.com")
        BusinessMembership.objects.create(
            business=self.business, user=self.staff_user, role=BusinessMembership.Role.STAFF
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.staff_user)
        self.raw_token = "ya29.this-is-a-very-real-looking-oauth-access-token"

    def test_raw_token_is_not_recoverable_from_the_db_column_directly(self):
        campaign = GoogleAdsCampaign.objects.create(
            business=self.business, name="Summer Promo", access_token=self.raw_token
        )

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT access_token FROM marketing_googleadscampaign WHERE id = %s", [str(campaign.id)]
            )
            raw_db_value = cursor.fetchone()[0]

        self.assertNotEqual(raw_db_value, self.raw_token)
        self.assertNotIn(self.raw_token, raw_db_value)

        # Only the model's decryption path (from_db_value) recovers it.
        reloaded = GoogleAdsCampaign.objects.get(pk=campaign.pk)
        self.assertEqual(reloaded.access_token, self.raw_token)

    def test_token_never_returned_by_the_api(self):
        create_response = self.client.post(
            campaign_list_url(self.business.id), {"name": "Winter Promo", "access_token": self.raw_token}
        )
        self.assertEqual(create_response.status_code, status.HTTP_201_CREATED, create_response.data)
        self.assertNotIn("access_token", create_response.data)
        self.assertNotIn("refresh_token", create_response.data)

        campaign_id = create_response.data["id"]
        get_response = self.client.get(campaign_detail_url(self.business.id, campaign_id))
        self.assertNotIn("access_token", get_response.data)

        # And the value is genuinely stored, encrypted, not silently dropped.
        campaign = GoogleAdsCampaign.objects.get(id=campaign_id)
        self.assertEqual(campaign.access_token, self.raw_token)

    def test_wrong_key_cannot_decrypt_a_previously_encrypted_value(self):
        campaign = GoogleAdsCampaign.objects.create(
            business=self.business, name="Spring Promo", access_token=self.raw_token
        )
        with patch("marketing.encryption.settings.FIELD_ENCRYPTION_KEY", _generate_fernet_key()):
            reloaded = GoogleAdsCampaign.objects.get(pk=campaign.pk)
            # Wrong key -> InvalidToken is caught and surfaced as empty,
            # not a decrypted (wrong) plaintext and not a raised exception
            # that would break every read of the row.
            self.assertEqual(reloaded.access_token, "")


class MarketingTenantIsolationTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(email="owner_mkt_iso@example.com")
        self.business_a = Business.objects.create(name="Mkt Biz A", owner=owner)
        self.business_b = Business.objects.create(name="Mkt Biz B", owner=owner)

        self.user_a = User.objects.create_user(email="staff_mkt_a@example.com")
        BusinessMembership.objects.create(
            business=self.business_a, user=self.user_a, role=BusinessMembership.Role.STAFF
        )
        other_user_b = User.objects.create_user(email="staff_mkt_b@example.com")
        BusinessMembership.objects.create(
            business=self.business_b, user=other_user_b, role=BusinessMembership.Role.STAFF
        )

        self.script_b = TrackingScript.objects.create(
            business=self.business_b, script_key=services.generate_script_key()
        )
        self.visitor_b = WebsiteVisitor.objects.create(business=self.business_b)
        self.lead_b = Lead.objects.create(business=self.business_b, name="Lead B", email="leadb@example.com")

        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_cannot_list_other_business_tracking_scripts(self):
        response = self.client.get(script_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_regenerate_other_business_script_key(self):
        old_key = self.script_b.script_key
        response = self.client.post(script_regenerate_url(self.business_b.id, self.script_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.script_b.refresh_from_db()
        self.assertEqual(self.script_b.script_key, old_key)

    def test_cannot_list_other_business_visitors(self):
        response = self.client.get(visitor_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_list_other_business_leads(self):
        response = self.client.get(lead_list_url(self.business_b.id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_delete_other_business_lead(self):
        response = self.client.delete(f"{lead_list_url(self.business_b.id)}{self.lead_b.id}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Lead.objects.filter(id=self.lead_b.id).exists())

    def test_cannot_create_campaign_for_other_business(self):
        response = self.client.post(campaign_list_url(self.business_b.id), {"name": "Sneaky"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(GoogleAdsCampaign.objects.filter(name="Sneaky").exists())
