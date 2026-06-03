import os
import uuid
import hmac
import json
import time
from datetime import datetime, timezone

os.environ.setdefault("ADMIN_KEY", "test-admin")
os.environ.setdefault("API_KEY_PEPPER", "test-pepper")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "test-stripe-secret")
os.environ["RESEND_API_KEY"] = ""
os.environ["FROM_EMAIL"] = ""

from fastapi.testclient import TestClient

import main


client = TestClient(main.app)


class Usage:
    def __init__(self, prompt_tokens=0, completion_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "offsite_backup_configured" in response.json()
    assert "offsite_backup_config" in response.json()
    assert response.json()["backup_scheduler"]["enabled"] is True


def test_landing_page_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "NexaFlow" in response.text
    assert "AI Business Ecosystem" in response.text
    assert "One link to your business tools." in response.text
    assert "一个入口" in response.text
    assert "Enquiries" in response.text
    assert "Follow-ups" in response.text
    assert "CRM" in response.text
    assert "Billing" in response.text
    assert "Inventory" in response.text
    assert "Automation" in response.text
    assert "Starter pricing" in response.text
    assert "30-day trial" in response.text
    assert "SGD 19" in response.text
    assert "SGD 49" in response.text
    assert "SGD 99+" in response.text
    assert "/ai-enquiry" in response.text
    assert "/merchant-login" in response.text
    assert "og:image" in response.text
    assert "nexaflow_home_lang" in response.text
    assert "<span>1</span>" not in response.text


def test_merchant_login_page_loads():
    response = client.get("/merchant-login")
    assert response.status_code == 200
    assert "Merchant Login" in response.text
    assert "Business slug" in response.text
    assert "Business access key" in response.text
    assert "nexaflow_business_key_" in response.text
    assert "/admin/dashboard" not in response.text


def test_pricing_page_loads():
    response = client.get("/pricing")
    assert response.status_code == 200
    assert "Pricing" in response.text
    assert "/terms" in response.text


def test_enquiry_app_pages_load():
    public_page = client.get("/ai-enquiry")
    admin_page = client.get("/enquiry-admin")
    legacy_public_page = client.get("/apps/enquiry")
    legacy_admin_page = client.get("/apps/enquiry/admin")

    assert public_page.status_code == 200
    assert "AI Enquiry Inbox" in public_page.text
    assert "submitEnquiry" in public_page.text
    assert "setProductLang" in public_page.text
    assert "AI WhatsApp enquiry assistant for local service businesses" in public_page.text
    assert "Data safety built in" in public_page.text
    assert "Consent before submit" in public_page.text
    assert "Private merchant inbox" in public_page.text
    assert "Delete customer enquiries" in public_page.text
    assert "View Pricing" in public_page.text
    assert "Merchant Login" in public_page.text
    assert "Start with a 30-day trial" in public_page.text
    assert "SGD 19" in public_page.text
    assert "/start-trial" in public_page.text
    assert "WhatsApp Us" in public_page.text
    assert "wa.me" in public_page.text
    assert "/assets/brand/nexaflow-final.png" in public_page.text
    assert "Built as a business ecosystem" in public_page.text
    assert "Enquiry" in public_page.text
    assert "Automation" in public_page.text
    assert "#2dd4bf" in public_page.text
    assert "#f3c76a" in public_page.text
    assert "radial-gradient(circle at 12% 10%" in public_page.text
    assert "repeating-linear-gradient(122deg" in public_page.text
    assert "Open Admin" not in public_page.text
    assert "/admin/dashboard" not in public_page.text
    brand_asset = client.get("/assets/brand/nexaflow-final.png")
    assert brand_asset.status_code == 200
    assert brand_asset.headers["content-type"].startswith("image/png")
    assert admin_page.status_code == 200
    assert "loadInbox" in admin_page.text
    assert "saveProfile" in admin_page.text
    assert "Trial requests" in admin_page.text
    assert "loadTrialRequests" in admin_page.text
    assert "createInboxFromTrial" in admin_page.text
    assert "Send Setup WhatsApp" in admin_page.text
    assert "/admin/dashboard" not in admin_page.text
    assert legacy_public_page.status_code == 200
    assert legacy_admin_page.status_code == 200

    trial_contact = client.get("/contact-trial", follow_redirects=False)
    assert trial_contact.status_code in {302, 307}
    assert "wa.me" in trial_contact.headers["location"]
    assert "30-day" in trial_contact.headers["location"]

    trial_page = client.get("/start-trial")
    assert trial_page.status_code == 200
    assert "Start your AI WhatsApp enquiry inbox" in trial_page.text
    assert "submitTrialRequest" in trial_page.text


def test_trial_request_flow():
    suffix = uuid.uuid4().hex[:8]
    missing_consent = client.post(
        "/apps/enquiry/api/trial-requests",
        json={
            "business_name": f"Trial Biz {suffix}",
            "contact_name": "Alex Trial",
            "whatsapp_phone": f"6599{suffix[:4]}",
            "business_type": "renovation",
            "pdpa_consent": False,
        },
    )
    assert missing_consent.status_code == 400

    create = client.post(
        "/apps/enquiry/api/trial-requests",
        json={
            "business_name": f"Trial Biz {suffix}",
            "contact_name": "Alex Trial",
            "contact_email": f"trial-{suffix}@example.com",
            "whatsapp_phone": f"6598{suffix[:4]}",
            "business_type": "renovation",
            "city": "Singapore",
            "monthly_enquiries": "31-100",
            "message": "We receive WhatsApp enquiries and need follow-up help.",
            "pdpa_consent": True,
        },
    )
    assert create.status_code == 200
    created = create.json()
    assert created["business_name"] == f"Trial Biz {suffix}"
    assert created["status"] == "new"

    blocked = client.get("/apps/enquiry/api/trial-requests")
    assert blocked.status_code in {401, 403}

    listed = client.get(
        "/apps/enquiry/api/trial-requests",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert listed.status_code == 200
    matching = [item for item in listed.json()["trial_requests"] if item["id"] == created["id"]]
    assert matching
    assert matching[0]["contact_email"] == f"trial-{suffix}@example.com"
    assert matching[0]["whatsapp_url"].startswith("https://wa.me/")

    updated = client.patch(
        f"/apps/enquiry/api/trial-requests/{created['id']}",
        json={"status": "contacted", "internal_note": "Sent setup checklist."},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert updated.status_code == 200
    assert updated.json()["status"] == "contacted"
    assert updated.json()["internal_note"] == "Sent setup checklist."

    profile = client.post(
        f"/apps/enquiry/api/trial-requests/{created['id']}/create-profile",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    payload = profile.json()
    assert payload["trial_request"]["status"] == "trial_setup"
    assert payload["profile"]["business_name"] == f"Trial Biz {suffix}"
    assert payload["profile"]["business_access_key"].startswith("biz_")
    assert payload["profile"]["form_url"].startswith("/enquiry/")
    assert payload["profile"]["inbox_url"].startswith("/inbox/")
    assert "Customer enquiry link" in payload["onboarding_message"]
    assert payload["profile"]["business_access_key"] in payload["onboarding_message"]
    assert payload["onboarding_whatsapp_url"].startswith("https://wa.me/")
    assert "Business%20access%20key" in payload["onboarding_whatsapp_url"]


def test_business_profile_create_and_public_form_loads():
    suffix = uuid.uuid4().hex[:8]
    slug = f"demo-reno-{suffix}"
    create = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Demo Reno",
            "business_type": "renovation",
            "whatsapp_phone": "6591234567",
            "offer_summary": "kitchen and bathroom renovation quotes",
            "reply_tone": "friendly",
            "opening_hours": "Mon-Fri 9am-6pm",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    assert create.json()["slug"] == slug
    assert create.json()["form_url"] == f"/enquiry/{slug}"
    assert create.json()["inbox_url"] == f"/inbox/{slug}"
    assert create.json()["embed_url"] == f"/embed/enquiry/{slug}.js"
    assert create.json()["business_access_key"].startswith("biz_")
    assert "access_key_hash" not in create.json()

    public_profile = client.get(f"/apps/enquiry/api/business-profiles/{slug}")
    assert public_profile.status_code == 200
    assert public_profile.json()["business_name"] == "Demo Reno"
    assert "business_access_key" not in public_profile.json()
    assert "access_key_prefix" not in public_profile.json()

    form = client.get(f"/enquiry/{slug}")
    assert form.status_code == 200
    assert "Demo Reno" in form.text
    assert "Business enquiry page" in form.text
    assert "kitchen and bathroom renovation quotes" in form.text
    assert "Send Enquiry" in form.text
    assert "pdpaConsent" in form.text
    assert "/privacy" in form.text
    assert slug in form.text
    assert "/admin/dashboard" not in form.text

    legacy_form = client.get(f"/apps/enquiry/form/{slug}")
    assert legacy_form.status_code == 200

    inbox = client.get(f"/inbox/{slug}")
    assert inbox.status_code == 200
    assert "loadMerchantInbox" in inbox.text
    assert "saveMerchantSettings" in inbox.text
    assert "exportMerchantCsv" in inbox.text
    assert "saveMerchantNote" in inbox.text
    assert "Customer enquiry link" in inbox.text
    assert "copyMerchantElement" in inbox.text
    assert "Website widget code" in inbox.text
    assert "1. Load inbox" in inbox.text
    assert "Pipeline Value" in inbox.text
    assert "filterPriority" in inbox.text
    assert "filterFollowUp" in inbox.text
    assert "Due Follow-ups" in inbox.text
    assert "clearMerchantFilters" in inbox.text
    assert "/admin/dashboard" not in inbox.text

    legacy_inbox = client.get(f"/apps/enquiry/inbox/{slug}")
    assert legacy_inbox.status_code == 200

    embed = client.get(f"/embed/enquiry/{slug}.js")
    assert embed.status_code == 200
    assert "application/javascript" in embed.headers["content-type"]
    assert "nexaflow-enquiry-widget" in embed.text
    assert f"/enquiry/{slug}" in embed.text
    assert "Demo Reno" in embed.text


def test_business_profile_requires_admin_key():
    response = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": "blocked-profile",
            "business_name": "Blocked",
            "whatsapp_phone": "6591234567",
        },
    )
    assert response.status_code == 401


def test_business_profile_onboarding_rotates_and_sends_key_without_exposing_secret():
    suffix = uuid.uuid4().hex[:8]
    slug = f"onboard-{suffix}"
    create = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Onboard Merchant",
            "business_type": "retail",
            "contact_email": "merchant@example.com",
            "whatsapp_phone": "6591234567",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    original_prefix = create.json()["access_key_prefix"]

    send = client.post(
        f"/apps/enquiry/api/business-profiles/{slug}/send-onboarding",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert send.status_code == 200
    body = send.json()
    assert body["rotated"] is True
    assert body["delivery"]["status"] == "pending_email_setup"
    assert body["profile"]["access_key_prefix"] != original_prefix
    assert "business_access_key" not in json.dumps(body)
    assert "access_key_hash" not in json.dumps(body)
    email_body = main.merchant_onboarding_email(main.get_business_profile(slug), "biz_test_key")
    assert "5-minute setup" in email_body
    assert "Website widget code" in email_body
    assert "Google Business Profile" in email_body


def test_business_profile_onboarding_requires_contact_email():
    suffix = uuid.uuid4().hex[:8]
    slug = f"no-email-{suffix}"
    create = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "No Email Merchant",
            "business_type": "retail",
            "whatsapp_phone": "6591234567",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    send = client.post(
        f"/apps/enquiry/api/business-profiles/{slug}/send-onboarding",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert send.status_code == 400


def test_enquiry_create_classifies_and_generates_whatsapp_reply():
    response = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "name": "Jamie",
            "phone": "+65 9123 4567",
            "email": "jamie@example.com",
            "business_type": "renovation",
            "message": "Need urgent quotation for this week. How much is your package?",
            "pdpa_consent": True,
            "source": "test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["intent"] == "quotation"
    assert body["priority"] == "hot"
    assert "reply_draft" not in body
    assert "whatsapp_url" not in body

    listing = client.get(
        "/apps/enquiry/api/enquiries",
        headers={"X-Admin-Key": "test-admin"},
    )
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == body["id"])
    assert "Jamie" in saved["reply_draft"]
    assert saved["whatsapp_url"].startswith("https://wa.me/6591234567")
    assert saved["pdpa_consent"] is True
    assert saved["consent_at"]


def test_enquiry_requires_pdpa_consent():
    response = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "name": "No Consent",
            "phone": "6591234567",
            "message": "Need repair quote",
            "source": "public-form",
        },
    )
    assert response.status_code == 400
    assert "Consent is required" in response.text


def test_enquiry_create_attaches_business_profile_and_owner_whatsapp():
    suffix = uuid.uuid4().hex[:8]
    slug = f"tuition-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Bright Tuition",
            "business_type": "tuition",
            "whatsapp_phone": "6588889999",
            "offer_summary": "primary math tuition",
            "reply_tone": "warm",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200

    response = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Mrs Lim",
            "phone": "6591112222",
            "business_type": "general",
            "message": "Can I book a trial lesson next week?",
            "pdpa_consent": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["business_slug"] == slug
    assert body["intent"] == "booking"
    assert "reply_draft" not in body

    listing = client.get(
        f"/apps/enquiry/api/enquiries?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == body["id"])
    assert saved["business_type"] == "tuition"
    assert "Bright Tuition" in saved["reply_draft"]
    assert saved["whatsapp_url"].startswith("https://wa.me/6588889999")
    assert saved["merchant_notification_status"] == "skipped"
    assert "contact_email" in saved["merchant_notification_error"]


def test_enquiry_create_notifies_business_contact_when_email_configured():
    suffix = uuid.uuid4().hex[:8]
    slug = f"notify-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Notify Merchant",
            "business_type": "repair",
            "contact_email": f"notify-{suffix}@example.com",
            "whatsapp_phone": "6588889999",
            "offer_summary": "repair enquiries",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200

    response = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Notify Lead",
            "phone": "6591112222",
            "message": "Need urgent repair quote today",
            "pdpa_consent": True,
        },
    )
    assert response.status_code == 200

    listing = client.get(
        f"/apps/enquiry/api/enquiries?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == response.json()["id"])
    assert saved["priority"] == "hot"
    assert saved["merchant_notification_status"] == "pending_email_setup"
    assert "RESEND_API_KEY" in saved["merchant_notification_error"]


def test_enquiry_admin_requires_key_and_can_update_status():
    create = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "name": "Priya",
            "phone": "6598887777",
            "business_type": "tuition",
            "message": "Can I book a class next Monday?",
            "pdpa_consent": True,
        },
    )
    assert create.status_code == 200
    enquiry_id = create.json()["id"]

    missing = client.get("/apps/enquiry/api/enquiries")
    assert missing.status_code == 401

    listing = client.get(
        "/apps/enquiry/api/enquiries",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert listing.status_code == 200
    assert listing.json()["stats"]["total"] >= 1
    assert any(item["id"] == enquiry_id for item in listing.json()["enquiries"])

    update = client.patch(
        f"/apps/enquiry/api/enquiries/{enquiry_id}",
        json={"status": "contacted"},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert update.status_code == 200
    assert update.json()["status"] == "contacted"


def test_enquiry_admin_can_filter_by_business_slug():
    suffix = uuid.uuid4().hex[:8]
    slug = f"filter-{suffix}"
    assert client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Filter Business",
            "business_type": "repair",
            "whatsapp_phone": "6599990000",
        },
        headers={"X-Admin-Key": "test-admin"},
    ).status_code == 200
    create = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Filter Lead",
            "phone": "6590001111",
            "message": "Need repair quotation",
            "pdpa_consent": True,
        },
    )
    assert create.status_code == 200
    listing = client.get(
        f"/apps/enquiry/api/enquiries?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert listing.status_code == 200
    assert listing.json()["stats"]["total"] >= 1
    assert all(item["business_slug"] == slug for item in listing.json()["enquiries"])


def test_merchant_key_can_only_access_own_enquiries_and_update_status():
    suffix = uuid.uuid4().hex[:8]
    slug_one = f"merchant-one-{suffix}"
    slug_two = f"merchant-two-{suffix}"
    profile_one = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_one,
            "business_name": "Merchant One",
            "business_type": "retail",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    profile_two = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_two,
            "business_name": "Merchant Two",
            "business_type": "beauty",
            "whatsapp_phone": "6592220000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile_one.status_code == 200
    assert profile_two.status_code == 200
    business_key = profile_one.json()["business_access_key"]

    lead_one = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug_one,
            "name": "Own Lead",
            "phone": "6591112222",
            "message": "Need price quote",
            "pdpa_consent": True,
        },
    )
    lead_two = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug_two,
            "name": "Other Lead",
            "phone": "6593334444",
            "message": "Need booking",
            "pdpa_consent": True,
        },
    )
    assert lead_one.status_code == 200
    assert lead_two.status_code == 200

    own_listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert own_listing.status_code == 200
    assert own_listing.json()["stats"]["total"] >= 1
    assert all(item["business_slug"] == slug_one for item in own_listing.json()["enquiries"])

    blocked_listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug_two}",
        headers={"X-Business-Key": business_key},
    )
    assert blocked_listing.status_code == 403

    own_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{lead_one.json()['id']}?business_slug={slug_one}",
        json={"status": "quoted"},
        headers={"X-Business-Key": business_key},
    )
    assert own_update.status_code == 200
    assert own_update.json()["status"] == "quoted"

    note_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{lead_one.json()['id']}?business_slug={slug_one}",
        json={
            "internal_note": "Quoted 800, follow up on Friday.",
            "follow_up_at": "Friday 3pm",
            "deal_value": 800,
        },
        headers={"X-Business-Key": business_key},
    )
    assert note_update.status_code == 200
    assert note_update.json()["status"] == "quoted"
    assert note_update.json()["internal_note"] == "Quoted 800, follow up on Friday."
    assert note_update.json()["follow_up_at"] == "Friday 3pm"
    assert note_update.json()["deal_value"] == 800

    blocked_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{lead_two.json()['id']}?business_slug={slug_one}",
        json={"status": "won"},
        headers={"X-Business-Key": business_key},
    )
    assert blocked_update.status_code == 404

    updated_listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert updated_listing.status_code == 200
    assert updated_listing.json()["stats"]["pipeline_value"] >= 800

    blocked_delete = client.delete(
        f"/apps/enquiry/api/merchant/enquiries/{lead_two.json()['id']}?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert blocked_delete.status_code == 404

    own_delete = client.delete(
        f"/apps/enquiry/api/merchant/enquiries/{lead_one.json()['id']}?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert own_delete.status_code == 200
    assert own_delete.json()["deleted"] is True

    after_delete = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert after_delete.status_code == 200
    assert all(item["id"] != lead_one.json()["id"] for item in after_delete.json()["enquiries"])


def test_merchant_inbox_includes_action_center_and_pipeline_board():
    suffix = uuid.uuid4().hex[:8]
    slug = f"inbox-ui-{suffix}"
    create = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Inbox UI Merchant",
            "business_type": "renovation",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    response = client.get(f"/inbox/{slug}")
    assert response.status_code == 200
    assert "merchantActionCenter" in response.text
    assert "merchantPipelineBoard" in response.text
    assert "Today&apos;s best action" in response.text
    assert "Trial readiness" in response.text
    assert "chooseNextAction" in response.text
    assert "Trial launch checklist" in response.text
    assert "merchantChecklist" in response.text
    assert "markChecklistStep" in response.text
    assert "nexaflow_trial_checklist_" in response.text
    assert "localStorage.setItem(`nexaflow_business_key_" in response.text


def test_merchant_can_update_own_business_settings_only():
    suffix = uuid.uuid4().hex[:8]
    slug_one = f"settings-one-{suffix}"
    slug_two = f"settings-two-{suffix}"
    profile_one = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_one,
            "business_name": "Settings One",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    profile_two = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_two,
            "business_name": "Settings Two",
            "business_type": "beauty",
            "whatsapp_phone": "6592220000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile_one.status_code == 200
    assert profile_two.status_code == 200
    business_key = profile_one.json()["business_access_key"]

    updated = client.patch(
        f"/apps/enquiry/api/merchant/profile?business_slug={slug_one}",
        json={
            "business_name": "Apex Repair",
            "business_type": "repair",
            "whatsapp_phone": "6588889999",
            "contact_email": "OWNER@EXAMPLE.COM",
            "offer_summary": "Emergency repair and maintenance.",
            "reply_tone": "fast and reassuring",
            "opening_hours": "Mon-Sat, 9am-7pm",
        },
        headers={"X-Business-Key": business_key},
    )
    assert updated.status_code == 200
    assert updated.json()["business_name"] == "Apex Repair"
    assert updated.json()["contact_email"] == "owner@example.com"
    assert updated.json()["access_key_prefix"] == profile_one.json()["access_key_prefix"]

    blocked = client.patch(
        f"/apps/enquiry/api/merchant/profile?business_slug={slug_two}",
        json={
            "business_name": "Wrong Business",
            "business_type": "beauty",
            "whatsapp_phone": "6577778888",
        },
        headers={"X-Business-Key": business_key},
    )
    assert blocked.status_code == 403

    lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug_one,
            "name": "Repair Buyer",
            "phone": "6512345678",
            "message": "Urgent repair quote please",
            "pdpa_consent": True,
        },
    )
    assert lead.status_code == 200
    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == lead.json()["id"])
    assert "6588889999" in saved["whatsapp_url"]


def test_merchant_can_export_own_enquiries_csv_only():
    suffix = uuid.uuid4().hex[:8]
    slug_one = f"export-one-{suffix}"
    slug_two = f"export-two-{suffix}"
    profile_one = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_one,
            "business_name": "Export One",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    profile_two = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_two,
            "business_name": "Export Two",
            "business_type": "beauty",
            "whatsapp_phone": "6592220000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile_one.status_code == 200
    assert profile_two.status_code == 200
    business_key = profile_one.json()["business_access_key"]

    lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug_one,
            "name": "CSV Buyer",
            "phone": "6599991111",
            "email": "csv@example.com",
            "message": "Need urgent quotation for repair",
            "pdpa_consent": True,
        },
    )
    assert lead.status_code == 200
    note = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{lead.json()['id']}?business_slug={slug_one}",
        json={
            "internal_note": "Export this note for the sales team.",
            "follow_up_at": "Tomorrow morning",
            "deal_value": 1200.50,
        },
        headers={"X-Business-Key": business_key},
    )
    assert note.status_code == 200

    missing_key = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug_one}"
    )
    assert missing_key.status_code == 401

    blocked = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug_two}",
        headers={"X-Business-Key": business_key},
    )
    assert blocked.status_code == 403

    exported = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment;" in exported.headers["content-disposition"]
    assert "CSV Buyer" in exported.text
    assert "csv@example.com" in exported.text
    assert "Need urgent quotation for repair" in exported.text
    assert "Export this note for the sales team." in exported.text
    assert "Tomorrow morning" in exported.text
    assert "1200.5" in exported.text


def test_merchant_can_filter_enquiries_and_exports():
    suffix = uuid.uuid4().hex[:8]
    slug = f"filter-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Filter Service",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    hot_quote = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Hot Quote Buyer",
            "phone": "6591112222",
            "message": "Need urgent quotation this week",
            "pdpa_consent": True,
        },
    )
    booking = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Booking Buyer",
            "phone": "6593334444",
            "message": "Can I book a slot next month?",
            "pdpa_consent": True,
        },
    )
    assert hot_quote.status_code == 200
    assert booking.status_code == 200

    filtered = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&priority=hot&intent=quotation&search=urgent",
        headers={"X-Business-Key": business_key},
    )
    assert filtered.status_code == 200
    names = [item["name"] for item in filtered.json()["enquiries"]]
    assert "Hot Quote Buyer" in names
    assert "Booking Buyer" not in names

    status_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{booking.json()['id']}?business_slug={slug}",
        json={"status": "contacted"},
        headers={"X-Business-Key": business_key},
    )
    assert status_update.status_code == 200

    status_filtered = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&status=contacted",
        headers={"X-Business-Key": business_key},
    )
    assert status_filtered.status_code == 200
    status_names = [item["name"] for item in status_filtered.json()["enquiries"]]
    assert "Booking Buyer" in status_names
    assert "Hot Quote Buyer" not in status_names

    exported = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug}&priority=hot&search=urgent",
        headers={"X-Business-Key": business_key},
    )
    assert exported.status_code == 200
    assert "Hot Quote Buyer" in exported.text
    assert "Booking Buyer" not in exported.text


def test_merchant_can_filter_due_followups():
    suffix = uuid.uuid4().hex[:8]
    slug = f"followup-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Followup Service",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    overdue = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Overdue Buyer",
            "phone": "6591112222",
            "message": "Need quotation soon",
            "pdpa_consent": True,
        },
    )
    future = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Future Buyer",
            "phone": "6593334444",
            "message": "Need repair booking",
            "pdpa_consent": True,
        },
    )
    no_follow = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "No Follow Buyer",
            "phone": "6595556666",
            "message": "General question",
            "pdpa_consent": True,
        },
    )
    assert overdue.status_code == 200
    assert future.status_code == 200
    assert no_follow.status_code == 200

    overdue_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{overdue.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2000-01-01", "deal_value": 500},
        headers={"X-Business-Key": business_key},
    )
    future_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{future.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2999-01-01", "deal_value": 900},
        headers={"X-Business-Key": business_key},
    )
    assert overdue_update.status_code == 200
    assert future_update.status_code == 200

    due = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&follow_up=due",
        headers={"X-Business-Key": business_key},
    )
    assert due.status_code == 200
    due_names = [item["name"] for item in due.json()["enquiries"]]
    assert "Overdue Buyer" in due_names
    assert "Future Buyer" not in due_names
    assert "No Follow Buyer" not in due_names
    assert due.json()["stats"]["due_followups"] >= 1

    scheduled = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&follow_up=scheduled",
        headers={"X-Business-Key": business_key},
    )
    scheduled_names = [item["name"] for item in scheduled.json()["enquiries"]]
    assert "Overdue Buyer" in scheduled_names
    assert "Future Buyer" in scheduled_names
    assert "No Follow Buyer" not in scheduled_names

    no_followups = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&follow_up=none",
        headers={"X-Business-Key": business_key},
    )
    no_follow_names = [item["name"] for item in no_followups.json()["enquiries"]]
    assert "No Follow Buyer" in no_follow_names
    assert "Overdue Buyer" not in no_follow_names

    exported_due = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug}&follow_up=due",
        headers={"X-Business-Key": business_key},
    )
    assert exported_due.status_code == 200
    assert "Overdue Buyer" in exported_due.text
    assert "Future Buyer" not in exported_due.text


def test_admin_can_send_due_followup_digest_preview():
    suffix = uuid.uuid4().hex[:8]
    slug = f"digest-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Digest Service",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
            "contact_email": f"digest-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    due_lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Digest Due Buyer",
            "phone": "6591112222",
            "message": "Need urgent quotation",
            "pdpa_consent": True,
        },
    )
    future_lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Digest Future Buyer",
            "phone": "6593334444",
            "message": "Need booking next month",
            "pdpa_consent": True,
        },
    )
    assert due_lead.status_code == 200
    assert future_lead.status_code == 200

    assert client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{due_lead.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2000-01-01", "internal_note": "Call before lunch."},
        headers={"X-Business-Key": business_key},
    ).status_code == 200
    assert client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{future_lead.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2999-01-01"},
        headers={"X-Business-Key": business_key},
    ).status_code == 200

    unauthorized = client.post(
        f"/apps/enquiry/api/followups/digest?business_slug={slug}&dry_run=true"
    )
    assert unauthorized.status_code == 401

    preview = client.post(
        f"/apps/enquiry/api/followups/digest?business_slug={slug}&dry_run=true",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert preview.status_code == 200
    data = preview.json()
    assert data["processed"] == 1
    assert data["sent"] == 1
    assert data["dry_run"] is True
    assert data["results"][0]["due_count"] == 1
    assert data["results"][0]["delivery"]["status"] == "dry_run"

    body = main.merchant_followup_digest_email(
        main.get_business_profile(slug),
        main.list_enquiry_records(business_slug=slug, follow_up="due"),
    )
    assert "Digest Due Buyer" in body
    assert "Call before lunch." in body
    assert "Digest Future Buyer" not in body


def test_public_enquiry_rate_limit_blocks_repeated_spam():
    suffix = uuid.uuid4().hex[:8]
    slug = f"rate-{suffix}"
    assert client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Rate Limited",
            "business_type": "repair",
            "whatsapp_phone": "6594440000",
        },
        headers={"X-Admin-Key": "test-admin"},
    ).status_code == 200

    payload = {
        "business_slug": slug,
        "name": "Repeat Lead",
        "phone": "6595550000",
        "message": "Need repair quote",
        "pdpa_consent": True,
    }
    for _ in range(10):
        assert client.post("/apps/enquiry/api/enquiries", json=payload).status_code == 200

    blocked = client.post("/apps/enquiry/api/enquiries", json=payload)
    assert blocked.status_code == 429


def test_legal_pages_load_and_are_linked():
    pages = [
        ("/terms", "Terms of Service"),
        ("/privacy", "Privacy Policy"),
        ("/refund-policy", "Refund Policy"),
        ("/acceptable-use", "Acceptable Use Policy"),
    ]

    home = client.get("/")
    assert home.status_code == 200
    assert "/privacy" in home.text

    for path, title in pages:
        response = client.get(path)
        assert response.status_code == 200
        assert title in response.text
        assert "nexaflowinfra@gmail.com" in response.text

    terms = client.get("/terms")
    assert "Merchant Responsibilities" in terms.text
    assert "Customer Enquiries and Personal Data" in terms.text
    assert "Limitation of Liability" in terms.text
    assert "Indemnity" in terms.text
    assert "does not guarantee that any enquiry will convert into a sale" in terms.text

    privacy = client.get("/privacy")
    assert "PDPA-Style Notice for Enquiry Forms" in privacy.text
    assert "consent timestamp" in privacy.text


def test_checkout_json_and_redirect_modes():
    os.environ["PAYMENT_LINK_STARTER"] = "https://buy.stripe.com/test_example"
    json_response = client.get("/billing/checkout?plan=starter&redirect=false")
    assert json_response.status_code == 200
    assert json_response.json()["payment_link"] == "https://buy.stripe.com/test_example"
    assert json_response.json()["success_url"].endswith("/billing/success?plan=starter")

    redirect_response = client.get("/billing/checkout?plan=starter", follow_redirects=False)
    assert redirect_response.status_code == 303
    assert redirect_response.headers["location"] == "https://buy.stripe.com/test_example"


def test_billing_result_pages_load():
    success = client.get("/billing/success?plan=starter")
    cancel = client.get("/billing/cancel?plan=starter")
    assert success.status_code == 200
    assert "Payment received" in success.text
    assert "Open Portal" in success.text
    assert "/portal" in success.text
    assert "Merchant setup" in success.text
    assert "Open Inbox" in success.text
    assert "Share Link" in success.text
    assert cancel.status_code == 200
    assert "Checkout cancelled" in cancel.text


def test_api_key_email_onboards_customer_to_portal_and_header_auth():
    body = main.delivery_email_body("customer_001", "nf_test_key", "starter", 10000)
    assert "Customer portal:" in body
    assert "/portal" in body
    assert "X-API-Key: nf_test_key" in body
    assert "/pricing" in body


def test_admin_dashboard_loads():
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    assert "Admin" in response.text
    assert "Payment Events" in response.text
    assert "Margin Guard" in response.text
    assert "resendApiKey" in response.text
    assert "Create Backup" in response.text
    assert "Test Offsite" in response.text
    assert "Revenue Report" in response.text
    assert "/admin/revenue-report" in response.text
    assert "Action Items" in response.text
    assert "/admin/action-items" in response.text


def test_customer_portal_loads():
    response = client.get("/portal")
    assert response.status_code == 200
    assert "Customer Portal" in response.text
    assert "X-API-Key" in response.text
    assert "sendTestRequest" in response.text
    assert "billingLinks" in response.text
    assert "openBillingPortal" in response.text
    assert "rotateCustomerKey" in response.text


def test_plans_are_public():
    response = client.get("/plans")
    assert response.status_code == 200
    assert "starter" in response.json()["plans"]


def test_admin_requires_key():
    response = client.get("/admin/clients")
    assert response.status_code == 401


def test_admin_revenue_report_requires_key():
    response = client.get("/admin/revenue-report")
    assert response.status_code == 401


def test_admin_action_items_requires_key():
    response = client.get("/admin/action-items")
    assert response.status_code == 401


def test_admin_revenue_report_summarizes_mrr_and_risk():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"report_customer_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "pro",
            "credits": 500,
            "billing_email": f"report-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    response = client.get("/admin/revenue-report", headers={"X-Admin-Key": "test-admin"})
    assert response.status_code == 200
    report = response.json()
    assert report["mrr"]["active_monthly_recurring_revenue_usd"] >= main.PLANS["pro"]["monthly_price_usd"]
    assert report["mrr"]["active_customers"] >= 1
    assert any(item["client_id"] == client_id for item in report["risk"]["low_credit_clients"])
    assert "month_to_date" in report["usage"]


def test_admin_action_items_flags_low_credit_customer():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"action_low_credit_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "starter",
            "credits": 50,
            "billing_email": f"action-low-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    response = client.get("/admin/action-items", headers={"X-Admin-Key": "test-admin"})
    assert response.status_code == 200
    assert response.json()["counts"]["total"] >= 1
    assert any(
        item["category"] == "credits" and item["client_id"] == client_id
        for item in response.json()["items"]
    )


def test_admin_action_items_flags_pending_delivery_notification():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"action_notify_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "starter",
            "credits": 50,
            "billing_email": f"action-notify-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    clients = main.load_clients()
    assert main.maybe_send_low_credit_notice(client_id, clients[client_id])["status"] == "pending_email_setup"

    response = client.get("/admin/action-items", headers={"X-Admin-Key": "test-admin"})
    assert response.status_code == 200
    assert any(
        item["category"] == "notification" and item["client_id"] == client_id
        for item in response.json()["items"]
    )


def test_customer_api_requires_key():
    response = client.get("/customer/me")
    assert response.status_code == 401


def test_customer_api_accepts_header_key_and_hides_secret():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"portal_customer_{suffix}",
            "plan": "starter",
            "billing_email": f"portal-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    api_key = create.json()["api_key"]

    me = client.get("/customer/me", headers={"X-API-Key": api_key})
    assert me.status_code == 200
    assert me.json()["client_id"] == f"portal_customer_{suffix}"
    assert "api_key" not in me.json()

    usage = client.get("/customer/usage", headers={"X-API-Key": api_key})
    assert usage.status_code == 200
    assert usage.json()["client_id"] == f"portal_customer_{suffix}"
    assert usage.json()["totals"]["requests"] == 0


def test_customer_billing_links_require_customer_key_and_expose_payment_links():
    os.environ["PAYMENT_LINK_STARTER"] = "https://buy.stripe.com/test_starter"
    os.environ["PAYMENT_LINK_PRO"] = "https://buy.stripe.com/test_pro"
    os.environ["PAYMENT_LINK_BUSINESS"] = "https://buy.stripe.com/test_business"
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"billing_customer_{suffix}",
            "plan": "starter",
            "billing_email": f"billing-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    missing = client.get("/customer/billing-links")
    assert missing.status_code == 401

    response = client.get(
        "/customer/billing-links",
        headers={"X-API-Key": create.json()["api_key"]},
    )
    assert response.status_code == 200
    assert response.json()["client"]["plan"] == "starter"
    assert response.json()["links"]["starter"]["payment_link"] == "https://buy.stripe.com/test_starter"
    assert response.json()["links"]["pro"]["checkout_url"] == "/billing/checkout?plan=pro"
    assert response.json()["billing_portal"]["available"] is False


def test_customer_can_self_setup_enquiry_business_profile():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"self_enquiry_{suffix}",
            "plan": "starter",
            "billing_email": f"self-enquiry-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    api_key = create.json()["api_key"]
    slug = f"self-enquiry-{suffix}"

    missing = client.get("/customer/enquiry/business-profiles")
    assert missing.status_code == 401

    profile = client.post(
        "/customer/enquiry/business-profiles",
        json={
            "slug": slug,
            "business_name": "Self Enquiry Merchant",
            "business_type": "retail",
            "contact_email": f"merchant-{suffix}@example.com",
            "whatsapp_phone": "6591234567",
            "offer_summary": "self-service enquiry setup",
        },
        headers={"X-API-Key": api_key},
    )
    assert profile.status_code == 200
    assert profile.json()["client_id"] == f"self_enquiry_{suffix}"
    assert profile.json()["form_url"] == f"/enquiry/{slug}"
    assert profile.json()["business_access_key"].startswith("biz_")

    listing = client.get(
        "/customer/enquiry/business-profiles",
        headers={"X-API-Key": api_key},
    )
    assert listing.status_code == 200
    assert len(listing.json()["profiles"]) == 1
    assert listing.json()["profiles"][0]["slug"] == slug


def test_customer_enquiry_profile_slug_is_isolated_between_customers():
    suffix = uuid.uuid4().hex[:8]
    slug = f"shared-slug-{suffix}"
    first = client.post(
        "/admin/clients",
        json={
            "client_id": f"owner_one_{suffix}",
            "plan": "starter",
            "billing_email": f"owner-one-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    second = client.post(
        "/admin/clients",
        json={
            "client_id": f"owner_two_{suffix}",
            "plan": "starter",
            "billing_email": f"owner-two-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert first.status_code == 200
    assert second.status_code == 200

    assert client.post(
        "/customer/enquiry/business-profiles",
        json={
            "slug": slug,
            "business_name": "First Owner",
            "business_type": "retail",
            "contact_email": f"first-{suffix}@example.com",
            "whatsapp_phone": "6591234567",
        },
        headers={"X-API-Key": first.json()["api_key"]},
    ).status_code == 200

    conflict = client.post(
        "/customer/enquiry/business-profiles",
        json={
            "slug": slug,
            "business_name": "Second Owner",
            "business_type": "retail",
            "contact_email": f"second-{suffix}@example.com",
            "whatsapp_phone": "6597654321",
        },
        headers={"X-API-Key": second.json()["api_key"]},
    )
    assert conflict.status_code == 409


def test_customer_can_send_own_enquiry_onboarding_only():
    suffix = uuid.uuid4().hex[:8]
    owner = client.post(
        "/admin/clients",
        json={
            "client_id": f"onboard_owner_{suffix}",
            "plan": "starter",
            "billing_email": f"onboard-owner-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    other = client.post(
        "/admin/clients",
        json={
            "client_id": f"onboard_other_{suffix}",
            "plan": "starter",
            "billing_email": f"onboard-other-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert owner.status_code == 200
    assert other.status_code == 200
    slug = f"owned-onboard-{suffix}"

    profile = client.post(
        "/customer/enquiry/business-profiles",
        json={
            "slug": slug,
            "business_name": "Owned Onboard",
            "business_type": "repair",
            "contact_email": f"owned-{suffix}@example.com",
            "whatsapp_phone": "6591234567",
        },
        headers={"X-API-Key": owner.json()["api_key"]},
    )
    assert profile.status_code == 200

    send = client.post(
        f"/customer/enquiry/business-profiles/{slug}/send-onboarding",
        headers={"X-API-Key": owner.json()["api_key"]},
    )
    assert send.status_code == 200
    assert send.json()["delivery"]["status"] == "pending_email_setup"
    assert "business_access_key" not in json.dumps(send.json())

    blocked = client.post(
        f"/customer/enquiry/business-profiles/{slug}/send-onboarding",
        headers={"X-API-Key": other.json()["api_key"]},
    )
    assert blocked.status_code == 404


def test_customer_billing_portal_requires_key():
    response = client.post("/customer/billing-portal")
    assert response.status_code == 401


def test_customer_billing_portal_requires_stripe_customer():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"portal_no_stripe_{suffix}",
            "plan": "starter",
            "billing_email": f"portal-no-stripe-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    response = client.post(
        "/customer/billing-portal",
        headers={"X-API-Key": create.json()["api_key"]},
    )
    assert response.status_code == 409


def test_customer_billing_portal_returns_configured_test_url():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"portal_stripe_{suffix}"
    old_url = os.environ.get("STRIPE_BILLING_PORTAL_TEST_URL")
    os.environ["STRIPE_BILLING_PORTAL_TEST_URL"] = "https://billing.stripe.com/p/session/test"
    try:
        _, api_key = main.create_client_record(
            client_id,
            "starter",
            10000,
            f"portal-stripe-{suffix}@example.com",
            stripe_customer_id=f"cus_{suffix}",
        )
        response = client.post(
            "/customer/billing-portal",
            headers={"X-API-Key": api_key},
        )
    finally:
        if old_url is None:
            os.environ.pop("STRIPE_BILLING_PORTAL_TEST_URL", None)
        else:
            os.environ["STRIPE_BILLING_PORTAL_TEST_URL"] = old_url

    assert response.status_code == 200
    assert response.json()["url"] == "https://billing.stripe.com/p/session/test"
    assert response.json()["test_mode"] is True


def test_customer_can_rotate_own_api_key_and_old_key_stops_working():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"self_rotate_{suffix}",
            "plan": "starter",
            "billing_email": f"self-rotate-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    old_api_key = create.json()["api_key"]
    old_prefix = create.json()["api_key_prefix"]

    rotated = client.post(
        "/customer/rotate-api-key",
        headers={"X-API-Key": old_api_key},
    )
    assert rotated.status_code == 200
    assert rotated.json()["rotated"] is True
    assert rotated.json()["api_key"].startswith("nf_")
    assert rotated.json()["api_key_prefix"] != old_prefix

    old_me = client.get("/customer/me", headers={"X-API-Key": old_api_key})
    assert old_me.status_code == 401

    new_me = client.get("/customer/me", headers={"X-API-Key": rotated.json()["api_key"]})
    assert new_me.status_code == 200
    assert new_me.json()["client_id"] == f"self_rotate_{suffix}"


def test_low_credit_notice_is_deduped_and_visible_to_admin():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"low_credit_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "starter",
            "credits": 999,
            "billing_email": f"low-credit-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    clients = main.load_clients()
    first = main.maybe_send_low_credit_notice(client_id, clients[client_id])
    second = main.maybe_send_low_credit_notice(client_id, clients[client_id])

    assert first["status"] == "pending_email_setup"
    assert first["threshold"] == 1000
    assert second["status"] == "already_sent"

    notifications = client.get(
        f"/admin/notifications?client_id={client_id}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert notifications.status_code == 200
    assert len(notifications.json()["notifications"]) == 1
    assert notifications.json()["notifications"][0]["notification_type"] == "low_credit"


def test_topup_resets_low_credit_notice():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"low_credit_reset_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "starter",
            "credits": 999,
            "billing_email": f"low-credit-reset-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    clients = main.load_clients()
    assert main.maybe_send_low_credit_notice(client_id, clients[client_id])["status"] == "pending_email_setup"

    topup = client.post(
        f"/admin/clients/{client_id}/topup",
        json={"amount": 5000},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert topup.status_code == 200

    notifications = client.get(
        f"/admin/notifications?client_id={client_id}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert notifications.status_code == 200
    assert notifications.json()["notifications"] == []


def test_deploy_check_requires_admin_key():
    response = client.get("/admin/deploy-check")
    assert response.status_code == 401


def test_deploy_check_loads_with_admin_key():
    response = client.get(
        "/admin/deploy-check",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert response.status_code == 200
    assert "checks" in response.json()
    assert any(check["name"] == "AUTO_BACKUP" for check in response.json()["checks"])


def test_offsite_backup_config_status_detects_partial_config():
    old_values = {key: os.environ.get(key) for key in [
        "S3_BACKUP_ENDPOINT_URL",
        "S3_BACKUP_BUCKET",
        "S3_BACKUP_ACCESS_KEY_ID",
        "S3_BACKUP_SECRET_ACCESS_KEY",
    ]}
    try:
        os.environ["S3_BACKUP_ENDPOINT_URL"] = "https://example.r2.cloudflarestorage.com"
        os.environ.pop("S3_BACKUP_BUCKET", None)
        os.environ.pop("S3_BACKUP_ACCESS_KEY_ID", None)
        os.environ.pop("S3_BACKUP_SECRET_ACCESS_KEY", None)
        status = main.offsite_backup_config_status()
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    assert status["configured"] is False
    assert status["partial"] is True
    assert "S3_BACKUP_BUCKET" in status["missing"]


def test_admin_backup_create_list_and_download():
    create = client.post("/admin/backups", headers={"X-Admin-Key": "test-admin"})
    assert create.status_code == 200
    backup_name = create.json()["backup"]["name"]
    assert create.json()["backup"]["offsite"]["status"] == "not_configured"
    assert (main.BACKUP_DIR / f"{backup_name}.json").exists()

    listing = client.get("/admin/backups", headers={"X-Admin-Key": "test-admin"})
    assert listing.status_code == 200
    assert "scheduler" in listing.json()
    listed_backup = next(item for item in listing.json()["backups"] if item["name"] == backup_name)
    assert listed_backup["offsite"]["status"] == "not_configured"

    download = client.get(
        f"/admin/backups/{backup_name}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert download.status_code == 200
    assert len(download.content) > 0


def test_admin_offsite_backup_test_reports_not_configured_without_customer_data():
    response = client.post("/admin/backups/offsite-test", headers={"X-Admin-Key": "test-admin"})
    assert response.status_code == 200
    assert response.json()["result"]["status"] == "not_configured"
    assert "does not upload customer data" in response.json()["note"]


def test_backup_retention_prunes_old_backups():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    created = [
        f"nexaflow-backup-{timestamp}001Z.db",
        f"nexaflow-backup-{timestamp}002Z.db",
        f"nexaflow-backup-{timestamp}003Z.db",
    ]
    main.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for name in created:
        (main.BACKUP_DIR / name).write_bytes(b"test-backup")

    pruned = main.prune_old_backups(retention_count=1)
    remaining = [backup["name"] for backup in main.list_sqlite_backups()]

    assert len(pruned) >= 2
    assert created[-1] in remaining
    assert created[0] not in remaining


def test_chat_requires_valid_key():
    response = client.post(
        "/v1/chat?api_key=bad",
        json={"message": "hello"},
    )
    assert response.status_code == 401


def test_chat_accepts_header_key_before_upstream_call():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"chat_header_{suffix}",
            "plan": "starter",
            "billing_email": f"chat-header-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    api_key = create.json()["api_key"]

    previous_openai = os.environ.pop("OPENAI_API_KEY", None)
    previous_openrouter = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        response = client.post(
            "/v1/chat",
            json={"message": "hello"},
            headers={"X-API-Key": api_key},
        )
    finally:
        if previous_openai is not None:
            os.environ["OPENAI_API_KEY"] = previous_openai
        if previous_openrouter is not None:
            os.environ["OPENROUTER_API_KEY"] = previous_openrouter

    assert response.status_code == 503
    assert "No configured provider" in response.text


def test_credit_calculation_uses_weighted_tokens():
    usage = Usage(prompt_tokens=1200, completion_tokens=300)
    assert main.calculate_credits_spent(usage) == 3


def test_credit_calculation_minimum_one_credit():
    usage = Usage(prompt_tokens=10, completion_tokens=1)
    assert main.calculate_credits_spent(usage) == 1


def test_usage_guard_blocks_request_larger_than_plan_cap():
    req = main.ChatRequest(message="hello", task="chat")
    plan = dict(main.PLANS["starter"])
    plan["max_request_credits"] = 0

    try:
        main.enforce_usage_guard(
            "usage_guard_cap",
            {"credits": 100, "status": "active", "plan": "starter"},
            plan,
            req,
        )
    except main.HTTPException as exc:
        assert exc.status_code == 413
        assert exc.detail["max_request_credits"] == 0
    else:
        raise AssertionError("Expected oversized request to be blocked")


def test_usage_guard_blocks_estimated_insufficient_credits():
    req = main.ChatRequest(message="hello", task="chat")

    try:
        main.enforce_usage_guard(
            "usage_guard_balance",
            {"credits": 0, "status": "active", "plan": "starter"},
            main.PLANS["starter"],
            req,
        )
    except main.HTTPException as exc:
        assert exc.status_code == 402
        assert exc.detail["remaining_credits"] == 0
    else:
        raise AssertionError("Expected insufficient credits to be blocked")


def test_usage_guard_blocks_daily_credit_limit():
    client_id = f"daily_limit_{uuid.uuid4().hex[:8]}"
    original_logs = main.load_logs()
    try:
        main.save_logs(
            original_logs
            + [
                {
                    "client_id": client_id,
                    "credits_spent": 1,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            ]
        )
        req = main.ChatRequest(message="hello", task="chat")
        plan = dict(main.PLANS["starter"])
        plan["daily_credit_limit"] = 1

        try:
            main.enforce_usage_guard(
                client_id,
                {"credits": 100, "status": "active", "plan": "starter"},
                plan,
                req,
            )
        except main.HTTPException as exc:
            assert exc.status_code == 429
            assert exc.detail["daily_credit_limit"] == 1
        else:
            raise AssertionError("Expected daily credit limit to be blocked")
    finally:
        main.save_logs(original_logs)


def test_provider_cost_calculation():
    model = {
        "input_usd_per_million": 1.00,
        "output_usd_per_million": 3.00,
    }
    assert main.calculate_provider_cost_usd(model, 1000, 500) == 0.0025


def test_model_router_uses_configured_default_provider():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = None
        provider = None
        routing_strategy = "profit"

    model_key, model, score = main.choose_model(Request(), main.PLANS["starter"])
    assert model_key == "gpt-4o-mini"
    assert model["provider"] == "openai"
    assert "estimated_gross_margin_usd" in score


def test_model_router_blocks_unavailable_tier():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = "gpt-4.1"
        provider = None
        routing_strategy = "profit"

    try:
        main.choose_model(Request(), main.PLANS["starter"])
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 403
    else:
        raise AssertionError("premium model should not be allowed on starter plan")


def test_profit_router_can_choose_openrouter_standard_for_pro_plan():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = None
        provider = None
        routing_strategy = "profit"

    model_key, model, score = main.choose_model(Request(), main.PLANS["pro"])
    assert model_key in {"gpt-4o-mini", "openrouter/auto"}
    assert model["tier"] in main.PLANS["pro"]["allowed_tiers"]
    assert score["estimated_revenue_usd"] > 0


def test_cost_router_chooses_lowest_estimated_cost():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = None
        provider = None
        routing_strategy = "cost"

    model_key, model, score = main.choose_model(Request(), main.PLANS["pro"])
    assert model_key == "gpt-4o-mini"
    assert model["provider"] == "openai"


def test_business_default_model_is_margin_safe():
    assert main.PLANS["business"]["default_model"] == "gpt-4o-mini"


def test_premium_model_blocked_when_margin_guard_fails():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = "gpt-4.1"
        provider = None
        routing_strategy = "profit"

    try:
        main.choose_model(Request(), main.PLANS["business"])
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 402
    else:
        raise AssertionError("premium model should be blocked when estimated margin is below guard")


def test_margin_report_has_viable_model_for_every_plan():
    report = main.plan_margin_report()
    assert all(any(model["meets_guard"] for model in models) for models in report.values())


def test_rank_model_candidates_returns_fallback_order():
    os.environ["OPENAI_API_KEY"] = "sk-test-key"
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test-key"

    class Request:
        message = "hello"
        task = "chat"
        model = None
        provider = None
        routing_strategy = "profit"

    candidates = main.rank_model_candidates(Request(), main.PLANS["pro"])
    assert len(candidates) >= 2
    assert candidates[0][2]["estimated_gross_margin_usd"] >= candidates[-1][2]["estimated_gross_margin_usd"]


def test_payment_webhook_requires_secret():
    response = client.post(
        "/webhooks/payment",
        json={
            "event_id": "test_event_missing_secret",
            "event_type": "payment.succeeded",
            "client_id": "webhook_customer_missing_secret",
            "plan": "starter",
        },
    )
    assert response.status_code == 401


def test_payment_webhook_ignores_duplicate_event():
    suffix = uuid.uuid4().hex[:8]
    event_id = f"test_event_duplicate_{suffix}"
    payload = {
        "event_id": event_id,
        "event_type": "payment.succeeded",
        "client_id": f"webhook_customer_{suffix}",
        "plan": "starter",
        "billing_email": "webhook@example.com",
        "amount_usd": 19,
        "mode": "subscription",
    }
    headers = {"X-Webhook-Secret": "test-webhook-secret"}

    first = client.post("/webhooks/payment", json=payload, headers=headers)
    second = client.post("/webhooks/payment", json=payload, headers=headers)

    assert first.status_code == 200
    assert first.json()["processed"] is True
    assert "api_key" in first.json()["result"]
    assert first.json()["result"]["delivery"]["status"] == "pending_email_setup"
    assert second.status_code == 200
    assert second.json()["processed"] is False
    assert second.json()["idempotent"] is True


def stripe_signature(payload, secret):
    timestamp = str(int(time.time()))
    signed_payload = f"{timestamp}.{payload}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), signed_payload, "sha256").hexdigest()
    return f"t={timestamp},v1={digest}"


def test_stripe_webhook_rejects_bad_signature():
    response = client.post(
        "/webhooks/stripe",
        content="{}",
        headers={"Stripe-Signature": "bad"},
    )
    assert response.status_code == 401


def test_stripe_webhook_processes_checkout_session():
    suffix = uuid.uuid4().hex[:8]
    event = {
        "id": f"evt_test_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer_details": {"email": "stripe@example.com"},
                "metadata": {
                    "client_id": f"stripe_customer_{suffix}",
                    "plan": "starter",
                    "mode": "subscription",
                },
            }
        },
    }
    payload = json.dumps(event, separators=(",", ":"))
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={
            "Stripe-Signature": stripe_signature(payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert response.json()["result"]["client_id"].startswith("stripe_customer_")
    assert "api_key" in response.json()["result"]
    assert response.json()["result"]["delivery"]["status"] == "pending_email_setup"
    enquiry_setup = response.json()["result"]["enquiry_setup"]
    assert enquiry_setup["created"] is True
    assert enquiry_setup["profile"]["client_id"] == f"stripe_customer_{suffix}"
    assert enquiry_setup["profile"]["form_url"].startswith("/enquiry/")
    assert "business_access_key" not in enquiry_setup["profile"]
    assert enquiry_setup["delivery"]["status"] == "pending_email_setup"

    profiles = client.get(
        "/customer/enquiry/business-profiles",
        headers={"X-API-Key": response.json()["result"]["api_key"]},
    )
    assert profiles.status_code == 200
    assert len(profiles.json()["profiles"]) == 1


def test_stripe_webhook_infers_plan_from_amount_without_metadata():
    suffix = uuid.uuid4().hex[:8]
    event = {
        "id": f"evt_test_amount_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 4900,
                "currency": "usd",
                "customer_details": {"email": f"amount-{suffix}@example.com"},
                "metadata": {},
            }
        },
    }
    payload = json.dumps(event, separators=(",", ":"))
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={
            "Stripe-Signature": stripe_signature(payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert response.json()["result"]["plan"] == "pro"


def test_initial_stripe_invoice_reuses_email_without_duplicate_credits():
    suffix = uuid.uuid4().hex[:8]
    email = f"stripe-dupe-{suffix}@example.com"
    checkout_event = {
        "id": f"evt_checkout_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }
    invoice_event = {
        "id": f"evt_invoice_{suffix}",
        "type": "invoice.paid",
        "data": {
            "object": {
                "amount_paid": 1900,
                "currency": "usd",
                "customer_email": email,
                "billing_reason": "subscription_create",
                "metadata": {},
            }
        },
    }

    checkout_payload = json.dumps(checkout_event, separators=(",", ":"))
    checkout_response = client.post(
        "/webhooks/stripe",
        content=checkout_payload,
        headers={
            "Stripe-Signature": stripe_signature(checkout_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert checkout_response.status_code == 200
    assert checkout_response.json()["result"]["credits"] == 10000

    invoice_payload = json.dumps(invoice_event, separators=(",", ":"))
    invoice_response = client.post(
        "/webhooks/stripe",
        content=invoice_payload,
        headers={
            "Stripe-Signature": stripe_signature(invoice_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert invoice_response.status_code == 200
    assert invoice_response.json()["result"]["client_id"] == checkout_response.json()["result"]["client_id"]
    assert invoice_response.json()["result"]["credits"] == 10000
    assert invoice_response.json()["result"]["credits_applied"] is False
    assert invoice_response.json()["result"]["enquiry_setup"]["created"] is False
    assert len(main.list_business_profiles(client_id=checkout_response.json()["result"]["client_id"])) == 1


def test_initial_stripe_invoice_without_billing_reason_does_not_duplicate_credits():
    suffix = uuid.uuid4().hex[:8]
    email = f"stripe-no-reason-{suffix}@example.com"
    checkout_event = {
        "id": f"evt_checkout_no_reason_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }
    invoice_event = {
        "id": f"evt_invoice_no_reason_{suffix}",
        "type": "invoice.paid",
        "data": {
            "object": {
                "amount_paid": 1900,
                "currency": "usd",
                "customer_email": email,
                "metadata": {},
            }
        },
    }

    checkout_payload = json.dumps(checkout_event, separators=(",", ":"))
    checkout_response = client.post(
        "/webhooks/stripe",
        content=checkout_payload,
        headers={
            "Stripe-Signature": stripe_signature(checkout_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert checkout_response.status_code == 200

    invoice_payload = json.dumps(invoice_event, separators=(",", ":"))
    invoice_response = client.post(
        "/webhooks/stripe",
        content=invoice_payload,
        headers={
            "Stripe-Signature": stripe_signature(invoice_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert invoice_response.status_code == 200
    assert invoice_response.json()["result"]["client_id"] == checkout_response.json()["result"]["client_id"]
    assert invoice_response.json()["result"]["credits"] == 10000
    assert invoice_response.json()["result"]["credits_applied"] is False


def test_initial_stripe_checkout_after_invoice_does_not_duplicate_credits():
    suffix = uuid.uuid4().hex[:8]
    email = f"stripe-reversed-{suffix}@example.com"
    invoice_event = {
        "id": f"evt_invoice_first_{suffix}",
        "type": "invoice.paid",
        "data": {
            "object": {
                "amount_paid": 1900,
                "currency": "usd",
                "customer_email": email,
                "metadata": {},
            }
        },
    }
    checkout_event = {
        "id": f"evt_checkout_second_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }

    invoice_payload = json.dumps(invoice_event, separators=(",", ":"))
    invoice_response = client.post(
        "/webhooks/stripe",
        content=invoice_payload,
        headers={
            "Stripe-Signature": stripe_signature(invoice_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert invoice_response.status_code == 200
    assert invoice_response.json()["result"]["credits"] == 10000

    checkout_payload = json.dumps(checkout_event, separators=(",", ":"))
    checkout_response = client.post(
        "/webhooks/stripe",
        content=checkout_payload,
        headers={
            "Stripe-Signature": stripe_signature(checkout_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert checkout_response.status_code == 200
    assert checkout_response.json()["result"]["client_id"] == invoice_response.json()["result"]["client_id"]
    assert checkout_response.json()["result"]["credits"] == 10000
    assert checkout_response.json()["result"]["credits_applied"] is False


def test_admin_payment_events_exposes_delivery_status():
    suffix = uuid.uuid4().hex[:8]
    email = f"delivery-status-{suffix}@example.com"
    event = {
        "id": f"evt_delivery_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }
    payload = json.dumps(event, separators=(",", ":"))
    response = client.post(
        "/webhooks/stripe",
        content=payload,
        headers={
            "Stripe-Signature": stripe_signature(payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200

    events = client.get(
        f"/admin/payment-events?billing_email={email}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert events.status_code == 200
    assert events.json()["payment_events"][0]["delivery_status"] == "pending_email_setup"


def test_admin_send_api_key_requires_rotation():
    suffix = uuid.uuid4().hex[:8]
    create = client.post(
        "/admin/clients",
        json={
            "client_id": f"rotate_customer_{suffix}",
            "plan": "starter",
            "billing_email": f"rotate-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200

    response = client.post(
        f"/admin/clients/rotate_customer_{suffix}/send-api-key",
        json={"rotate": False},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert response.status_code == 400


def test_admin_send_api_key_rotates_and_reports_delivery_status():
    suffix = uuid.uuid4().hex[:8]
    client_id = f"delivery_customer_{suffix}"
    create = client.post(
        "/admin/clients",
        json={
            "client_id": client_id,
            "plan": "starter",
            "billing_email": f"delivery-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    old_prefix = create.json()["api_key_prefix"]

    response = client.post(
        f"/admin/clients/{client_id}/send-api-key",
        json={"rotate": True},
        headers={"X-Admin-Key": "test-admin"},
    )
    assert response.status_code == 200
    assert response.json()["rotated"] is True
    assert response.json()["api_key_prefix"] != old_prefix
    assert response.json()["delivery"]["status"] == "pending_email_setup"


def test_stripe_payment_failed_pauses_client_by_subscription_id():
    suffix = uuid.uuid4().hex[:8]
    email = f"failed-{suffix}@example.com"
    subscription_id = f"sub_{suffix}"
    checkout_event = {
        "id": f"evt_checkout_lifecycle_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer": f"cus_{suffix}",
                "subscription": subscription_id,
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }
    failed_event = {
        "id": f"evt_failed_{suffix}",
        "type": "invoice.payment_failed",
        "data": {
            "object": {
                "amount_due": 1900,
                "currency": "usd",
                "customer": f"cus_{suffix}",
                "subscription": subscription_id,
                "customer_email": email,
                "metadata": {},
            }
        },
    }

    checkout_payload = json.dumps(checkout_event, separators=(",", ":"))
    assert client.post(
        "/webhooks/stripe",
        content=checkout_payload,
        headers={
            "Stripe-Signature": stripe_signature(checkout_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    ).status_code == 200

    failed_payload = json.dumps(failed_event, separators=(",", ":"))
    response = client.post(
        "/webhooks/stripe",
        content=failed_payload,
        headers={
            "Stripe-Signature": stripe_signature(failed_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert response.json()["result"]["status"] == "paused"
    assert response.json()["result"]["delivery"]["status"] == "pending_email_setup"

    notifications = client.get(
        f"/admin/notifications?client_id={response.json()['result']['client_id']}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert notifications.status_code == 200
    assert any(
        item["notification_type"] == "payment_action_required"
        for item in notifications.json()["notifications"]
    )


def test_stripe_subscription_deleted_cancels_client_by_customer_id():
    suffix = uuid.uuid4().hex[:8]
    email = f"cancel-{suffix}@example.com"
    customer_id = f"cus_cancel_{suffix}"
    subscription_id = f"sub_cancel_{suffix}"
    checkout_event = {
        "id": f"evt_checkout_cancel_{suffix}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "amount_total": 1900,
                "currency": "usd",
                "customer": customer_id,
                "subscription": subscription_id,
                "customer_details": {"email": email},
                "metadata": {},
            }
        },
    }
    deleted_event = {
        "id": f"evt_deleted_{suffix}",
        "type": "customer.subscription.deleted",
        "data": {
            "object": {
                "id": subscription_id,
                "customer": customer_id,
                "status": "canceled",
                "metadata": {},
            }
        },
    }

    checkout_payload = json.dumps(checkout_event, separators=(",", ":"))
    assert client.post(
        "/webhooks/stripe",
        content=checkout_payload,
        headers={
            "Stripe-Signature": stripe_signature(checkout_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    ).status_code == 200

    deleted_payload = json.dumps(deleted_event, separators=(",", ":"))
    response = client.post(
        "/webhooks/stripe",
        content=deleted_payload,
        headers={
            "Stripe-Signature": stripe_signature(deleted_payload, "test-stripe-secret"),
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert response.json()["result"]["status"] == "cancelled"
    assert response.json()["result"]["delivery"]["status"] == "pending_email_setup"

    notifications = client.get(
        f"/admin/notifications?client_id={response.json()['result']['client_id']}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert notifications.status_code == 200
    assert any(
        item["notification_type"] == "subscription_cancelled"
        for item in notifications.json()["notifications"]
    )
