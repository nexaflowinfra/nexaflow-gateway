import os
import uuid
import hmac
import json
import time
from datetime import datetime, timezone, timedelta

os.environ.setdefault("ADMIN_KEY", "test-admin")
os.environ.setdefault("API_KEY_PEPPER", "test-pepper")
os.environ.setdefault("PAYMENT_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "test-stripe-secret")
os.environ.setdefault("META_WEBHOOK_VERIFY_TOKEN", "test-meta-verify-token")
os.environ.setdefault("META_APP_SECRET", "test-meta-app-secret")
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
    assert "One inbox for every customer enquiry." in response.text
    assert "所有客户询问，进一个 inbox。" in response.text
    assert "who to reply first" in response.text
    assert "what the customer needs" in response.text
    assert "What your sales team sees every day" in response.text
    assert "All messages in one queue" in response.text
    assert "Know what is missing" in response.text
    assert "Next reply ready" in response.text
    assert "Bring customer messages into the queue" in response.text
    assert "Paste or screenshot when needed" in response.text
    assert "Reply or set reminder" in response.text
    assert "Create Enquiry Inbox" in response.text
    assert "WhatsApp Quote Lead" in response.text
    assert "Customer needs" in response.text
    assert "Simple pricing after trial" in response.text
    assert "30-day trial" in response.text
    assert "Full AI follow-up" in response.text
    assert "Up to 100 enquiries / month" in response.text
    assert "AI priority, category, and stuck-point detection" in response.text
    assert "Next reply drafts and follow-up reminders" in response.text
    assert "101-500 enquiries / month" in response.text
    assert "Shared team queue and follow-up dashboard" in response.text
    assert "500+ enquiries / month or custom volume" in response.text
    assert "SGD 49" in response.text
    assert "SGD 89" in response.text
    assert "SGD 149+" in response.text
    assert "Singapore" in response.text
    assert "Malaysia" in response.text
    assert "MYR 169" in response.text
    assert "MYR 299" in response.text
    assert "MYR 499+" in response.text
    assert "/merchant-signup" in response.text
    assert "nexaflow_home_market" in response.text
    assert "/merchant-login" in response.text
    assert "og:image" in response.text
    assert 'rel="icon"' in response.text
    assert "/assets/brand/nexaflow-icon.png" in response.text
    assert 'alt="NexaFlow logo"' in response.text
    assert "nexaflow_home_lang" in response.text
    assert "/dealer-demo" in response.text
    assert "All car buyer DMs" not in response.text
    assert "car buyer" not in response.text
    assert "Create Dealer Inbox" not in response.text
    assert "Dealer Login" not in response.text
    assert "Civic" not in response.text
    assert "Vios" not in response.text
    assert "Mazda" not in response.text
    assert "车商" not in response.text
    assert "买车" not in response.text
    assert "二手车" not in response.text
    assert "看车" not in response.text
    assert "<span>1</span>" not in response.text
    icon_asset = client.get("/assets/brand/nexaflow-icon.png")
    assert icon_asset.status_code == 200
    assert icon_asset.headers["content-type"].startswith("image/png")
    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/png")
    demo_video = client.get("/assets/demo/nexaflow-dealer-walkthrough.webm")
    assert demo_video.status_code == 200
    assert demo_video.headers["content-type"].startswith("video/webm")


def test_public_dealer_demo_is_read_only_and_synthetic(monkeypatch):
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    response = client.get("/dealer-demo")
    assert response.status_code == 200
    assert "A sales queue for used car enquiries." in response.text
    assert "二手车询问的销售队列。" in response.text
    assert "setDealerDemoLang" in response.text
    assert "nexaflow_dealer_demo_lang" in response.text
    assert "今天的买家队列" in response.text
    assert "Today&amp;apos;s buyer queue" not in response.text
    assert "Open today&amp;apos;s queue" not in response.text
    assert "TikTok Civic Buyer" in response.text
    assert "Referral Trade-in Buyer" in response.text
    assert "demoLeadDetail" in response.text
    assert "demo-queue-card" in response.text
    assert "Buyer wants" in response.text
    assert "客户想要" in response.text
    assert "Stuck on" in response.text
    assert "客户卡点" in response.text
    assert "Next reply" in response.text
    assert "下一句回复" in response.text
    assert "More buyer details" in response.text
    assert "更多买家资料" in response.text
    assert "Demo notes" in response.text
    assert "Demo 说明" in response.text
    assert "Demo data only. No message is sent." in response.text
    assert "Buyer progress" in response.text
    assert "买家进度" in response.text
    assert "Create Dealer Inbox" in response.text
    assert "创建车商 Inbox" in response.text
    assert "/admin/dashboard" not in response.text
    assert "business_access_key" not in response.text
    assert "nexaflow_business_key_" not in response.text
    assert "X-Admin-Key" not in response.text
    assert "/webhooks/meta" not in response.text
    assert "Download Buyer List" not in response.text
    assert "exportMerchantCsv" not in response.text
    assert "deleteMerchantLead" not in response.text
    assert "setMerchantStatus" not in response.text
    assert "/apps/enquiry/api/merchant/enquiries/export.csv" not in response.text


def test_merchant_login_page_loads():
    response = client.get("/merchant-login")
    assert response.status_code == 200
    assert "Dealer Login" in response.text
    assert "Dealer link name" in response.text
    assert "Inbox password" in response.text
    assert "nexaflow_business_key_" in response.text
    assert "/merchant-signup" in response.text
    assert "/admin/dashboard" not in response.text


def test_merchant_signup_page_and_api_create_workspace():
    page = client.get("/merchant-signup")
    assert page.status_code == 200
    assert "Create your buyer inbox" in page.text
    assert "setSignupLang" in page.text
    assert "nexaflow_signup_lang" in page.text
    assert "创建你的买家 inbox" in page.text
    assert "WhatsApp number (required)" in page.text
    assert "WhatsApp 号码（必填）" in page.text
    assert "WhatsApp number is required." in page.text
    assert "车行 / 展厅名称" in page.text
    assert "不需要社媒密码" in page.text
    assert "createMerchantWorkspace" in page.text
    assert "signupErrorMessage" in page.text
    assert "No social media password needed." in page.text
    assert "Optional setup" in page.text
    assert page.text.index("Create your buyer inbox") < page.text.index("Optional setup")
    assert "/apps/enquiry/api/merchant/signup" in page.text
    assert "/admin/dashboard" not in page.text

    suffix = str(uuid.uuid4())[:8]
    slug = f"dealer-signup-{suffix}"
    created = client.post(
        "/apps/enquiry/api/merchant/signup",
        json={
            "business_name": "Dealer Signup Auto",
            "whatsapp_phone": "60123456789",
            "contact_email": f"dealer-{suffix}@example.com",
            "business_type": "used_car_dealer",
            "market": "my",
            "preferred_slug": slug,
            "monthly_enquiries": "50_200",
            "pdpa_consent": True,
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["profile"]["slug"] == slug
    assert body["profile"]["business_type"] == "used_car_dealer"
    assert body["business_access_key"].startswith("biz_")
    assert "business_access_key" not in body["profile"]
    assert body["inbox_url"] == f"/inbox/{slug}"
    assert body["channels_url"] == f"/channels/{slug}"
    assert "Never paste social media passwords" in body["security_notice"]

    inbox_api = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        headers={"X-Business-Key": body["business_access_key"]},
    )
    assert inbox_api.status_code == 200
    assert inbox_api.json()["business"]["slug"] == slug

    no_email_slug = f"dealer-signup-no-email-{suffix}"
    no_email = client.post(
        "/apps/enquiry/api/merchant/signup",
        json={
            "business_name": "Dealer Signup No Email",
            "whatsapp_phone": "60123456780",
            "contact_email": "",
            "business_type": "used_car_dealer",
            "market": "my",
            "preferred_slug": no_email_slug,
            "monthly_enquiries": "under_50",
            "pdpa_consent": True,
        },
    )
    assert no_email.status_code == 200
    assert no_email.json()["profile"]["slug"] == no_email_slug
    assert no_email.json()["profile"]["contact_email"] is None

    conflict = client.post(
        "/apps/enquiry/api/merchant/signup",
        json={
            "business_name": "Another Dealer",
            "whatsapp_phone": "60129876543",
            "contact_email": f"another-{suffix}@example.com",
            "preferred_slug": slug,
            "pdpa_consent": True,
        },
    )
    assert conflict.status_code == 409

    no_consent = client.post(
        "/apps/enquiry/api/merchant/signup",
        json={
            "business_name": "No Consent Dealer",
            "whatsapp_phone": "60121112222",
            "contact_email": f"noconsent-{suffix}@example.com",
            "preferred_slug": f"no-consent-{suffix}",
            "pdpa_consent": False,
        },
    )
    assert no_consent.status_code == 400


def test_merchant_signup_rate_limits_same_contact():
    suffix = str(uuid.uuid4())[:8]
    email = f"signup-limit-{suffix}@example.com"
    for index in range(3):
        response = client.post(
            "/apps/enquiry/api/merchant/signup",
            json={
                "business_name": f"Rate Limit Dealer {index}",
                "whatsapp_phone": "60125550123",
                "contact_email": email,
                "preferred_slug": f"rate-limit-dealer-{suffix}-{index}",
                "pdpa_consent": True,
            },
        )
        assert response.status_code == 200

    blocked = client.post(
        "/apps/enquiry/api/merchant/signup",
        json={
            "business_name": "Rate Limit Dealer Blocked",
            "whatsapp_phone": "60125550123",
            "contact_email": email,
            "preferred_slug": f"rate-limit-dealer-{suffix}-blocked",
            "pdpa_consent": True,
        },
    )
    assert blocked.status_code == 429


def test_admin_dashboard_includes_backend_automation_panel():
    response = client.get("/admin/dashboard")
    assert response.status_code == 200
    assert "Backend Automation" in response.text
    assert "automationStatus" in response.text
    assert "runAutomation(true, false)" in response.text
    assert "runAutomation(true, true)" in response.text
    assert "runAutomation(false, true)" in response.text
    assert "/admin/automation/run" in response.text
    assert "renderAutomationResult" in response.text
    assert "Merchant Health" in response.text
    assert "/admin/merchant-health" in response.text
    assert "merchantHealthRows" in response.text
    assert "Data Protection" in response.text
    assert "runRetentionCleanup(true)" in response.text
    assert "runRetentionCleanup(false)" in response.text
    assert "/admin/data-retention/cleanup" in response.text
    assert "/admin/data-audit-events" in response.text
    assert "auditEvents" in response.text


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
    assert "One enquiry link. One private inbox. Faster WhatsApp follow-up." in public_page.text
    assert "Data safety built in" in public_page.text
    assert "Consent before submit" in public_page.text
    assert "Private dealer inbox" in public_page.text
    assert "Delete buyer enquiries" in public_page.text
    assert "Create My Buyer Inbox" in public_page.text
    assert "Dealer Login" in public_page.text
    assert "Start with a 30-day trial" in public_page.text
    assert "Full AI follow-up" in public_page.text
    assert "Up to 100 enquiries / month" in public_page.text
    assert "101-500 enquiries / month" in public_page.text
    assert "500+ enquiries / month or custom volume" in public_page.text
    assert "SGD 49" in public_page.text
    assert "SGD 89" in public_page.text
    assert "Singapore" in public_page.text
    assert "Malaysia" in public_page.text
    assert "MYR 169" in public_page.text
    assert "MYR 299" in public_page.text
    assert "MYR 499+" in public_page.text
    assert "nexaflow_enquiry_market" in public_page.text
    assert "/merchant-signup" in public_page.text
    assert "WhatsApp Us" in public_page.text
    assert "wa.me" in public_page.text
    assert "/assets/brand/nexaflow-final.png" in public_page.text
    assert "/assets/brand/nexaflow-icon.png" in public_page.text
    assert 'alt="NexaFlow logo"' in public_page.text
    assert "Simple follow-up flow for car dealers" in public_page.text
    assert "Buyer asks" in public_page.text
    assert "Dealer follows up" in public_page.text
    assert "#45d5c7" in public_page.text
    assert "#f3c76a" in public_page.text
    assert "linear-gradient(180deg, #070707" in public_page.text
    assert "linear-gradient(90deg, rgba(255,255,255,.028)" in public_page.text
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
    assert "trialRequestStats" in admin_page.text
    assert "Urgent Follow-up" in admin_page.text
    assert "Ending Soon" in admin_page.text
    assert "Conversion Due" in admin_page.text
    assert "Upgrade WhatsApp" in admin_page.text
    assert "Prepare Upgrade WhatsApp" in admin_page.text
    assert "Readiness" in admin_page.text
    assert "Trial End" in admin_page.text
    assert "Next action" in admin_page.text
    assert "/admin/dashboard" not in admin_page.text
    assert legacy_public_page.status_code == 200
    assert legacy_admin_page.status_code == 200

    trial_contact = client.get("/contact-trial", follow_redirects=False)
    assert trial_contact.status_code in {302, 307}
    assert "wa.me" in trial_contact.headers["location"]
    assert "30-day" in trial_contact.headers["location"]

    trial_page = client.get("/start-trial")
    assert trial_page.status_code == 200
    assert "Create your enquiry link." in trial_page.text
    assert "submitTrialRequest" in trial_page.text
    assert "trialLeadSource" in trial_page.text


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
            "lead_source": "gmail",
            "campaign": "acra-batch-1",
            "referrer": "https://www.linkedin.com/company/nexaflow",
            "message": "We receive WhatsApp enquiries and need follow-up help.",
            "pdpa_consent": True,
        },
    )
    assert create.status_code == 200
    created = create.json()
    assert created["business_name"] == f"Trial Biz {suffix}"
    assert created["status"] == "new"
    assert created["lead_source"] == "gmail"
    assert created["campaign"] == "acra-batch-1"

    blocked = client.get("/apps/enquiry/api/trial-requests")
    assert blocked.status_code in {401, 403}

    listed = client.get(
        "/apps/enquiry/api/trial-requests",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert listed.status_code == 200
    assert "stats" in listed.json()
    assert listed.json()["stats"]["total"] >= 1
    matching = [item for item in listed.json()["trial_requests"] if item["id"] == created["id"]]
    assert matching
    assert matching[0]["contact_email"] == f"trial-{suffix}@example.com"
    assert matching[0]["lead_source"] == "gmail"
    assert matching[0]["campaign"] == "acra-batch-1"
    assert matching[0]["referrer"] == "https://www.linkedin.com/company/nexaflow"
    assert matching[0]["whatsapp_url"].startswith("https://wa.me/")
    assert matching[0]["age_days"] >= 0
    assert matching[0]["days_until_trial_end"] is None
    assert matching[0]["follow_up_priority"] in {"low", "medium", "high"}
    assert "WhatsApp" in matching[0]["next_action"] or "Contact" in matching[0]["next_action"]

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
    assert payload["trial_request"]["trial_started_at"]
    assert payload["trial_request"]["trial_ends_at"]
    assert 29 <= payload["trial_request"]["days_until_trial_end"] <= 30
    assert payload["trial_request"]["conversion_stage"] == "active_trial"
    assert payload["trial_request"]["conversion_next_action"]
    assert payload["trial_request"]["conversion_plan_links"]["starter"].startswith("https://")
    assert payload["trial_request"]["conversion_whatsapp_url"].startswith("https://wa.me/")
    assert payload["profile"]["business_name"] == f"Trial Biz {suffix}"
    assert payload["profile"]["business_access_key"].startswith("biz_")
    assert payload["profile"]["form_url"].startswith("/enquiry/")
    assert payload["profile"]["inbox_url"].startswith("/inbox/")
    assert "Buyer enquiry link" in payload["onboarding_message"]
    assert payload["profile"]["business_access_key"] in payload["onboarding_message"]
    assert payload["onboarding_whatsapp_url"].startswith("https://wa.me/")
    assert "Inbox%20password" in payload["onboarding_whatsapp_url"]
    assert "Starter:" in payload["conversion_message"]
    assert payload["conversion_whatsapp_url"].startswith("https://wa.me/")

    listed_after_setup = client.get(
        "/apps/enquiry/api/trial-requests",
        headers={"X-Admin-Key": "test-admin"},
    )
    matching_after_setup = [
        item for item in listed_after_setup.json()["trial_requests"] if item["id"] == created["id"]
    ]
    assert matching_after_setup[0]["trial_ends_at"]
    assert matching_after_setup[0]["conversion_stage"] == "active_trial"
    assert matching_after_setup[0]["conversion_whatsapp_url"].startswith("https://wa.me/")
    assert "ending_soon" in listed_after_setup.json()["stats"]
    assert "conversion_due" in listed_after_setup.json()["stats"]

    expired_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with main.db_connection() as connection:
        connection.execute(
            "UPDATE trial_requests SET trial_ends_at = ?, updated_at = ? WHERE id = ?",
            (expired_at, datetime.now(timezone.utc).isoformat(), created["id"]),
        )

    listed_expired = client.get(
        "/apps/enquiry/api/trial-requests",
        headers={"X-Admin-Key": "test-admin"},
    )
    expired_match = [
        item for item in listed_expired.json()["trial_requests"] if item["id"] == created["id"]
    ][0]
    assert expired_match["conversion_stage"] == "trial_ended"
    assert expired_match["follow_up_priority"] == "high"
    assert expired_match["conversion_whatsapp_url"].startswith("https://wa.me/")
    assert listed_expired.json()["stats"]["trial_ended"] >= 1
    assert listed_expired.json()["stats"]["conversion_due"] >= 1


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
    assert "loadDemoBuyers" in inbox.text
    assert "Load Demo Buyers" in inbox.text
    assert "Today&apos;s Buyer Follow-up" in inbox.text
    assert "setInboxLang" in inbox.text
    assert "nexaflow_inbox_lang" in inbox.text
    assert "今日买家跟进" in inbox.text
    assert "现在要跟进的买家" in inbox.text
    assert "下载买家列表" in inbox.text
    assert "资料保留天数" in inbox.text
    assert "贷款 / 供车" in inbox.text
    assert "月供" in inbox.text
    assert "Buyers to contact now" in inbox.text
    assert "merchantDailyLeads" in inbox.text
    assert "saveMerchantSettings" in inbox.text
    assert "exportMerchantCsv" in inbox.text
    assert "/channels/" in inbox.text
    assert "saveMerchantNote" in inbox.text
    assert "Buyer enquiry link" in inbox.text
    assert "copyMerchantElement" in inbox.text
    assert "Website widget code" in inbox.text
    assert "Today&apos;s buyer follow-up" in inbox.text
    assert "Shortcuts" in inbox.text
    assert "Advanced tools: social sources, buyer link, setup, and settings" in inbox.text
    assert "Social source inbox" in inbox.text
    assert "Assisted capture" in inbox.text
    assert "Next move" in inbox.text
    assert "TikTok" in inbox.text
    assert "Xiaohongshu" in inbox.text
    assert "Main buyer link" in inbox.text
    assert "Copy Buyer Link" in inbox.text
    assert "Buyer progress" in inbox.text
    assert "Recorded sale value" in inbox.text
    assert "filterPriority" in inbox.text
    assert "filterSource" in inbox.text
    assert "filterFollowUp" in inbox.text
    assert "Due today" in inbox.text
    assert "clearMerchantFilters" in inbox.text
    assert inbox.text.index("Buyers to contact now") < inbox.text.index("Full buyer list and filters")
    assert inbox.text.index("Buyers to contact now") < inbox.text.index("Data Retention Days")
    assert "/admin/dashboard" not in inbox.text

    legacy_inbox = client.get(f"/apps/enquiry/inbox/{slug}")
    assert legacy_inbox.status_code == 200

    channels = client.get(f"/channels/{slug}")
    assert channels.status_code == 200
    assert "Social Source Setup" in channels.text
    assert "setChannelsLang" in channels.text
    assert "nexaflow_channels_lang" in channels.text
    assert "询问来源设置" in channels.text
    assert "不要在这里粘贴平台密码" in channels.text
    assert "申请自动同步" in channels.text
    assert "Never paste platform passwords" in channels.text
    assert "Request auto sync" in channels.text
    assert "Assisted capture" in channels.text
    assert "Meta auto-sync setup" in channels.text
    assert "metaSetupContent" in channels.text
    assert "loadMetaSetup" in channels.text
    assert "/admin/dashboard" not in channels.text

    legacy_channels = client.get(f"/apps/enquiry/channels/{slug}")
    assert legacy_channels.status_code == 200

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
    assert "auto_summary" not in body
    assert "next_action" not in body
    assert "follow_up_recommendation" not in body

    listing = client.get(
        "/apps/enquiry/api/enquiries",
        headers={"X-Admin-Key": "test-admin"},
    )
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == body["id"])
    assert "Jamie" in saved["reply_draft"]
    assert saved["whatsapp_url"].startswith("https://wa.me/6591234567")
    assert saved["pdpa_consent"] is True
    assert saved["consent_at"]
    assert saved["follow_up_at"] == datetime.now(timezone.utc).date().isoformat()
    assert "Jamie sent" in saved["auto_summary"]
    assert "quotation" in saved["auto_summary"]
    assert "urgent" in saved["auto_summary"]
    assert "Reply as soon as possible" in saved["next_action"]
    assert "Follow up" in saved["follow_up_recommendation"]

    email = main.merchant_enquiry_notification_email(main.default_enquiry_profile(), saved)
    assert "Auto-organized summary" in email
    assert "Recommended next action" in email
    assert "Auto follow-up date" in email
    assert saved["auto_summary"] in email


def test_vehicle_enquiry_identifies_sales_followup_signals():
    suffix = uuid.uuid4().hex[:8]
    slug = f"dealer-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "City Used Cars",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "60123456789",
            "offer_summary": "used car sales, loan support, and viewing appointments",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    response = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Alex",
            "phone": "+60 12-345 6789",
            "business_type": "used_car_dealer",
            "message": "Saw the Civic on TikTok. Can loan? monthly below RM900. I am comparing the same car with another dealer, can view today?",
            "source": "tiktok",
            "campaign": "civic-video",
            "pdpa_consent": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["intent"] == "quotation"
    assert response.json()["priority"] == "hot"

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=tiktok",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = listing.json()["enquiries"][0]
    signal_labels = {item["label"] for item in saved["follow_up_signals"]}
    assert "Finance / loan" in signal_labels
    assert "Monthly payment" in signal_labels
    assert "Price comparison" in signal_labels
    assert "Viewing / appointment" in signal_labels
    assert saved["source"] == "tiktok"
    assert saved["analysis_source"] == "rules_v1"
    assert saved["stuck_point"] == "Monthly payment or loan readiness"
    assert "target monthly payment" in saved["next_action"]
    assert "loan support" in saved["reply_draft"]
    assert "Signals:" in saved["auto_summary"]
    assert saved["follow_up_at"] == datetime.now(timezone.utc).date().isoformat()


def test_analyze_enquiry_returns_complete_vehicle_followup_plan():
    profile = {
        **main.default_enquiry_profile(),
        "business_name": "Analysis Dealer",
        "business_type": "used_car_dealer",
        "offer_summary": "used cars with loan support",
        "auto_followup_enabled": True,
        "hot_followup_hours": 2,
        "standard_followup_days": 1,
    }
    analysis = main.analyze_enquiry(
        "Instagram Buyer",
        "used_car_dealer",
        "Saw your Civic on Instagram. Can loan? Monthly below RM900 and I am comparing another dealer. Can view today?",
        profile,
    )
    signal_keys = {item["key"] for item in analysis["signals"]}
    assert analysis["analysis_source"] == "rules_v1"
    assert analysis["classification"]["intent"] == "quotation"
    assert analysis["classification"]["priority"] == "hot"
    assert {"finance", "monthly_payment", "comparison", "appointment"}.issubset(signal_keys)
    assert analysis["guidance"]["stuck_point"] == "Monthly payment or loan readiness"
    assert "target monthly payment" in analysis["workflow"]["next_action"]
    assert analysis["follow_up_at"] == datetime.now(timezone.utc).date().isoformat()
    assert "target monthly payment" in analysis["reply_draft"]


def test_analyze_enquiry_can_use_optional_ai_structured_output(monkeypatch):
    calls = []

    class FakeCompletions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {
                                "message": type(
                                    "Message",
                                    (),
                                    {
                                        "content": json.dumps(
                                            {
                                                "intent": "booking",
                                                "priority": "hot",
                                                "estimated_value": "high",
                                                "signals": [{"key": "appointment"}],
                                                "stuck_point": "Viewing time is not confirmed yet",
                                                "next_question": "Ask what time they can come to the showroom today.",
                                                "follow_up_timing": "Follow up the same day if they do not confirm.",
                                                "auto_summary": "Buyer wants to view the car and needs a fast appointment follow-up.",
                                                "next_action": "Confirm viewing time before discussing more options.",
                                                "follow_up_recommendation": "Send one same-day reminder if there is no reply.",
                                                "reply_draft": "Hi, what time can you come to view the car today?",
                                            }
                                        )
                                    },
                                )()
                            },
                        )()
                    ]
                },
            )()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    monkeypatch.setenv("NEXAFLOW_ENQUIRY_AI_ANALYSIS_ENABLED", "true")
    monkeypatch.setattr(main, "get_provider_client", lambda provider: FakeClient())
    analysis = main.analyze_enquiry(
        "AI Buyer",
        "used_car_dealer",
        "Can view the Civic today? Need loan also.",
        {**main.default_enquiry_profile(), "business_type": "used_car_dealer"},
    )
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert analysis["analysis_source"] == "ai:gpt-4o-mini"
    assert analysis["classification"]["intent"] == "booking"
    assert analysis["classification"]["priority"] == "hot"
    assert analysis["guidance"]["stuck_point"] == "Viewing time is not confirmed yet"
    assert analysis["signals"][0]["key"] == "appointment"
    assert analysis["reply_draft"].startswith("Hi, what time")


def test_analyze_enquiry_falls_back_when_ai_output_is_invalid(monkeypatch):
    class BadCompletions:
        def create(self, **kwargs):
            return type(
                "Response",
                (),
                {
                    "choices": [
                        type(
                            "Choice",
                            (),
                            {"message": type("Message", (), {"content": "not-json"})()},
                        )()
                    ]
                },
            )()

    class BadClient:
        chat = type("Chat", (), {"completions": BadCompletions()})()

    monkeypatch.setenv("NEXAFLOW_ENQUIRY_AI_ANALYSIS_ENABLED", "true")
    monkeypatch.setattr(main, "get_provider_client", lambda provider: BadClient())
    analysis = main.analyze_enquiry(
        "Fallback Buyer",
        "used_car_dealer",
        "Can loan? Monthly below RM900.",
        {**main.default_enquiry_profile(), "business_type": "used_car_dealer"},
    )
    assert analysis["analysis_source"] == "rules_v1"
    assert analysis["guidance"]["stuck_point"] == "Monthly payment or loan readiness"


def test_carpet_care_enquiry_does_not_trigger_vehicle_followup():
    assert main.vehicle_sales_context("Need carpet care quotation", "repair") is False
    classification = main.classify_enquiry("Need carpet care quotation", "repair")
    draft = main.enquiry_reply_draft(
        "Casey",
        "repair",
        "Need carpet care quotation",
        classification,
        {
            **main.default_enquiry_profile(),
            "business_name": "Care Repair",
            "business_type": "repair",
            "offer_summary": "carpet care and cleaning",
        },
    )
    assert "carpet care" in draft
    assert "loan support" not in draft
    assert "monthly payment" not in draft
    assert "view the car" not in draft


def test_enquiry_tracks_marketing_attribution_and_source_stats():
    suffix = uuid.uuid4().hex[:8]
    slug = f"attrib-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Attribution Merchant",
            "business_type": "repair",
            "whatsapp_phone": "6591234567",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200

    created = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Meta Lead",
            "phone": "6591112222",
            "message": "Need urgent repair quotation today",
            "source": "instagram",
            "campaign": "june-reels-2",
            "referrer": "https://www.instagram.com/nexaflowinfra",
            "page_url": "https://merchant.example.com/contact",
            "pdpa_consent": True,
        },
    )
    assert created.status_code == 200

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        headers={"X-Business-Key": profile.json()["business_access_key"]},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == created.json()["id"])
    assert saved["source"] == "instagram"
    assert saved["campaign"] == "june-reels-2"
    assert saved["referrer"] == "https://www.instagram.com/nexaflowinfra"
    assert saved["page_url"] == "https://merchant.example.com/contact"
    assert listing.json()["stats"]["by_source"]["instagram"] >= 1

    exported = client.get(
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug}",
        headers={"X-Business-Key": profile.json()["business_access_key"]},
    )
    assert exported.status_code == 200
    assert "instagram" in exported.text
    assert "june-reels-2" in exported.text
    assert "https://merchant.example.com/contact" not in exported.text


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


def test_enquiry_create_attaches_business_profile_and_buyer_whatsapp():
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
    assert saved["whatsapp_url"].startswith("https://wa.me/6591112222")
    assert "6588889999" not in saved["whatsapp_url"]
    assert saved["merchant_notification_status"] == "skipped"
    assert "contact_email" in saved["merchant_notification_error"]


def test_public_business_profile_hides_internal_fields():
    suffix = uuid.uuid4().hex[:8]
    slug = f"public-profile-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Public Profile Merchant",
            "business_type": "beauty",
            "whatsapp_phone": "6591234567",
            "contact_email": "owner@example.com",
            "offer_summary": "Beauty enquiries",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200

    public = client.get(f"/apps/enquiry/api/business-profiles/{slug}")
    assert public.status_code == 200
    body = public.json()
    assert body["business_name"] == "Public Profile Merchant"
    assert body["form_url"] == f"/enquiry/{slug}"
    assert body["embed_url"] == f"/embed/enquiry/{slug}.js"
    assert "contact_email" not in body
    assert "whatsapp_phone" not in body
    assert "access_key_prefix" not in body
    assert "client_id" not in body


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

    unauthorized_audit = client.get("/admin/data-audit-events")
    assert unauthorized_audit.status_code == 401

    audit = client.get(
        f"/admin/data-audit-events?business_slug={slug_one}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    event_types = {item["event_type"] for item in events}
    assert {"enquiry.created", "enquiry.updated", "enquiry.deleted"}.issubset(event_types)
    audit_payload = json.dumps(events)
    assert "Own Lead" not in audit_payload
    assert "6591112222" not in audit_payload


def test_merchant_can_manually_capture_social_dm_with_followup_guidance():
    suffix = uuid.uuid4().hex[:8]
    slug = f"manual-lead-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Manual Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
            "offer_summary": "used cars with loan support",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    missing_ack = client.post(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        json={
            "name": "TikTok Buyer",
            "phone": "60123456789",
            "source": "tiktok",
            "message": "Saw Civic on TikTok. Can loan? monthly below RM900, can view today?",
            "processing_acknowledged": False,
        },
        headers={"X-Business-Key": business_key},
    )
    assert missing_ack.status_code == 400

    created = client.post(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        json={
            "name": "TikTok Buyer",
            "phone": "60123456789",
            "source": "tiktok",
            "campaign": "Civic DM",
            "message": "Saw Civic on TikTok. Can loan? monthly below RM900, can view today?",
            "processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["business_slug"] == slug
    assert body["source"] == "tiktok"
    assert body["campaign"] == "Civic DM"
    assert body["merchant_notification_status"] == "not_required"
    assert body["analysis_source"] == "rules_v1"
    assert body["stuck_point"] == "Monthly payment or loan readiness"
    assert "monthly payment" in body["next_question"]
    assert "2 hours" in body["follow_up_timing"]
    assert body["whatsapp_url"].startswith("https://wa.me/60123456789")
    assert "Merchant confirmed" in body["consent_notice"]

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=tiktok",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if item["id"] == body["id"])
    assert saved["stuck_point"] == "Monthly payment or loan readiness"
    assert "loan support" in saved["reply_draft"]
    assert "Follow-up timing" not in saved["message"]

    social_only = client.post(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        json={
            "name": "",
            "phone": "",
            "source": "instagram",
            "campaign": "Vios IG DM",
            "message": "Saw your Vios on Instagram. Lowest deposit can? I am comparing with another dealer.",
            "processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert social_only.status_code == 200
    social_body = social_only.json()
    assert social_body["name"] == "Instagram Buyer"
    assert social_body["phone"].startswith("instagram:")
    assert social_body["source"] == "instagram"
    assert social_body["whatsapp_url"] is None
    assert social_body["stuck_point"] in {
        "Monthly payment or loan readiness",
        "Comparing price, spec, or monthly payment with another dealer",
    }

    audit = client.get(
        f"/admin/data-audit-events?business_slug={slug}&event_type=enquiry.created",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert any(item["actor_type"] == "merchant_manual" for item in events)
    audit_payload = json.dumps(events)
    assert "TikTok Buyer" not in audit_payload
    assert "60123456789" not in audit_payload


def test_merchant_can_load_demo_buyers_with_owner_key_only():
    suffix = uuid.uuid4().hex[:8]
    slug = f"demo-seed-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Demo Seed Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
            "offer_summary": "used cars with loan and trade-in support",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    unauthorized = client.post(
        f"/apps/enquiry/api/merchant/demo-enquiries?business_slug={slug}"
    )
    assert unauthorized.status_code == 401

    created = client.post(
        f"/apps/enquiry/api/merchant/demo-enquiries?business_slug={slug}",
        headers={"X-Business-Key": business_key},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["status"] == "created"
    assert body["created"] == 7
    assert "loan, monthly payment, viewing, and comparison" in body["message"]

    duplicate = client.post(
        f"/apps/enquiry/api/merchant/demo-enquiries?business_slug={slug}",
        headers={"X-Business-Key": business_key},
    )
    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "already_loaded"
    assert duplicate.json()["created"] == 0
    assert duplicate.json()["existing"] == 7

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    enquiries = listing.json()["enquiries"]
    demo_records = [item for item in enquiries if item["referrer"] == "nexaflow-demo-pack"]
    assert len(demo_records) == 7
    assert any(item["source"] == "tiktok" and item["priority"] == "hot" for item in demo_records)
    assert any(item["source"] == "referral" for item in demo_records)
    assert any(item["status"] == "won" for item in demo_records)
    assert all(not item["whatsapp_url"] for item in demo_records)

    audit = client.get(
        f"/admin/data-audit-events?business_slug={slug}&event_type=enquiry.created",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert any(item["actor_type"] == "merchant_demo" for item in events)
    audit_payload = json.dumps(events)
    assert "TikTok Civic Buyer" not in audit_payload
    assert "demo-tiktok-001" not in audit_payload


def test_merchant_manual_capture_blocks_secrets_and_sensitive_documents():
    suffix = uuid.uuid4().hex[:8]
    slug = f"manual-safe-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Manual Safe Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    secret = client.post(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        json={
            "name": "Secret Buyer",
            "phone": "60123456789",
            "message": "Buyer sent password=abc123 and OTP 778899",
            "processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert secret.status_code == 400
    assert "passwords" in secret.text

    document = client.post(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        json={
            "name": "Document Buyer",
            "phone": "60123456789",
            "message": "Buyer sent bank statement and payslip for loan check",
            "processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert document.status_code == 400
    assert "sensitive files" in document.text


def test_merchant_channel_connections_are_private_and_audited():
    suffix = uuid.uuid4().hex[:8]
    slug_one = f"channels-one-{suffix}"
    slug_two = f"channels-two-{suffix}"
    phone_number_id = f"phone-number-id-{suffix}"
    profile_one = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_one,
            "business_name": "Channels One",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    profile_two = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_two,
            "business_name": "Channels Two",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6022223333",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile_one.status_code == 200
    assert profile_two.status_code == 200
    business_key = profile_one.json()["business_access_key"]
    business_key_two = profile_two.json()["business_access_key"]

    unauthorized = client.get(
        f"/apps/enquiry/api/merchant/channel-connections?business_slug={slug_one}"
    )
    assert unauthorized.status_code == 401

    listing = client.get(
        f"/apps/enquiry/api/merchant/channel-connections?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    body = listing.json()
    assert body["data_protection"]["tokens_stored"] is False
    assert body["data_protection"]["owner_key_required"] is True
    assert "passwords, OTPs, cookies" in body["security_notice"]
    assert {item["channel"] for item in body["connections"]}.issuperset({"whatsapp", "instagram", "facebook", "tiktok", "xiaohongshu"})
    connections_by_channel = {item["channel"]: item for item in body["connections"]}
    assert connections_by_channel["whatsapp"]["id_field"]["meta_name"] == "phone_number_id"
    assert connections_by_channel["whatsapp"]["id_field"]["matched_from"] == "value.metadata.phone_number_id"
    assert connections_by_channel["facebook"]["id_field"]["meta_name"] == "page_id"
    assert connections_by_channel["instagram"]["id_field"]["meta_name"] == "instagram_account_id"

    meta_setup_missing = client.get(
        f"/apps/enquiry/api/merchant/meta-setup?business_slug={slug_one}"
    )
    assert meta_setup_missing.status_code == 401

    meta_setup = client.get(
        f"/apps/enquiry/api/merchant/meta-setup?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert meta_setup.status_code == 200
    meta_body = meta_setup.json()
    assert meta_body["webhook"]["url"] == "https://api.nexaflowinfra.com/webhooks/meta"
    assert meta_body["webhook"]["verify_token_configured"] is True
    assert meta_body["webhook"]["app_secret_configured"] is True
    assert meta_body["webhook"]["signature_required"] is True
    assert meta_body["webhook"]["https_callback_url"] is True
    assert meta_body["webhook"]["ready_for_meta_setup"] is True
    assert "site_url_configured" in meta_body["webhook"]
    assert meta_body["security"]["tokens_stored"] is False
    assert "test-meta" not in json.dumps(meta_body)
    assert {item["channel"] for item in meta_body["meta_channels"]} == {"whatsapp", "facebook", "instagram"}
    meta_channels = {item["channel"]: item for item in meta_body["meta_channels"]}
    assert meta_channels["whatsapp"]["id_field"]["label"] == "WhatsApp phone_number_id"
    assert meta_channels["whatsapp"]["id_field"]["matched_from"] == "value.metadata.phone_number_id"
    assert meta_channels["facebook"]["id_field"]["meta_name"] == "page_id"
    assert meta_channels["instagram"]["id_field"]["meta_name"] == "instagram_account_id"

    rejected_secret = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/whatsapp?business_slug={slug_one}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "City Cars WABA",
            "external_account_id": phone_number_id,
            "data_processing_acknowledged": True,
            "notes": "Prepare Cloud API setup. access_token=should-not-be-used",
            "access_token": "secret-token-that-should-be-ignored",
        },
        headers={"X-Business-Key": business_key},
    )
    assert rejected_secret.status_code == 400

    update = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/whatsapp?business_slug={slug_one}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "City Cars WABA",
            "external_account_id": phone_number_id,
            "data_processing_acknowledged": True,
            "notes": "Prepare Cloud API setup with server-side OAuth later.",
            "access_token": "secret-token-that-should-be-ignored",
        },
        headers={"X-Business-Key": business_key},
    )
    assert update.status_code == 200
    saved = update.json()
    assert saved["channel"] == "whatsapp"
    assert saved["token_status"] == "not_stored"
    assert "access_token" not in json.dumps(saved)
    assert "secret-token" not in json.dumps(saved)

    duplicate_external_id = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/whatsapp?business_slug={slug_two}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "Other Dealer WABA",
            "external_account_id": phone_number_id,
            "data_processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key_two},
    )
    assert duplicate_external_id.status_code == 409

    blocked = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/instagram?business_slug={slug_two}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "data_processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert blocked.status_code == 403

    limited_official = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/tiktok?business_slug={slug_one}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "data_processing_acknowledged": True,
        },
        headers={"X-Business-Key": business_key},
    )
    assert limited_official.status_code == 400

    missing_ack = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/facebook?business_slug={slug_one}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "data_processing_acknowledged": False,
        },
        headers={"X-Business-Key": business_key},
    )
    assert missing_ack.status_code == 400

    audit = client.get(
        f"/admin/data-audit-events?business_slug={slug_one}&event_type=channel.connection_updated",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert audit.status_code == 200
    events = audit.json()["events"]
    assert events
    audit_payload = json.dumps(events)
    assert "whatsapp" in audit_payload
    assert "not_stored" in audit_payload
    assert "secret-token" not in audit_payload
    assert "access_token" not in audit_payload


def test_merchant_meta_setup_readiness_requires_meta_secrets_and_https(monkeypatch):
    profile = {"slug": "readiness-demo", "business_name": "Readiness Demo"}

    monkeypatch.setenv("META_WEBHOOK_VERIFY_TOKEN", "verify-token")
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    missing_secret = main.merchant_meta_setup_response(profile)
    assert missing_secret["webhook"]["verify_token_configured"] is True
    assert missing_secret["webhook"]["app_secret_configured"] is False
    assert missing_secret["webhook"]["ready_for_meta_setup"] is False

    monkeypatch.setenv("META_APP_SECRET", "app-secret")
    monkeypatch.setenv("NEXAFLOW_SITE_URL", "http://localhost:8000")
    insecure_url = main.merchant_meta_setup_response(profile)
    assert insecure_url["webhook"]["https_callback_url"] is False
    assert insecure_url["webhook"]["ready_for_meta_setup"] is False


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
    assert "merchantDailyLeads" in response.text
    assert "Buyers to contact now" in response.text
    assert "Phone / handle optional" in response.text
    assert "买家还没给电话号码" in response.text
    assert "merchantPipelineBoard" in response.text
    assert "Today&apos;s buyer follow-up" in response.text
    assert "Today&apos;s numbers" in response.text
    assert "Shortcuts" in response.text
    assert "Main buyer link" in response.text
    assert "Suggested caption" in response.text
    assert "merchantShareDirectPrimary" in response.text
    assert "Advanced tools: social sources, buyer link, setup, and settings" in response.text
    assert "Full buyer list and filters" in response.text
    assert "Buyer progress" in response.text
    assert "chooseNextAction" in response.text
    assert "Dealer setup checklist" in response.text
    assert "merchantChecklist" in response.text
    assert "markChecklistStep" in response.text
    assert "nexaflow_trial_checklist_" in response.text
    assert "localStorage.setItem(`nexaflow_business_key_" in response.text
    assert "settingsAutoFollowup" in response.text
    assert "settingsDataRetentionDays" in response.text


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
            "auto_followup_enabled": False,
            "hot_followup_hours": 2,
            "standard_followup_days": 3,
            "data_retention_days": 180,
        },
        headers={"X-Business-Key": business_key},
    )
    assert updated.status_code == 200
    assert updated.json()["business_name"] == "Apex Repair"
    assert updated.json()["contact_email"] == "owner@example.com"
    assert updated.json()["auto_followup_enabled"] is False
    assert updated.json()["hot_followup_hours"] == 2
    assert updated.json()["standard_followup_days"] == 3
    assert updated.json()["data_retention_days"] == 180
    assert updated.json()["access_key_prefix"] == profile_one.json()["access_key_prefix"]

    loaded_profile = client.get(
        f"/apps/enquiry/api/merchant/profile?business_slug={slug_one}",
        headers={"X-Business-Key": business_key},
    )
    assert loaded_profile.status_code == 200
    assert loaded_profile.json()["business_name"] == "Apex Repair"
    assert loaded_profile.json()["contact_email"] == "owner@example.com"

    blocked_profile = client.get(
        f"/apps/enquiry/api/merchant/profile?business_slug={slug_two}",
        headers={"X-Business-Key": business_key},
    )
    assert blocked_profile.status_code == 403

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
    assert "6512345678" in saved["whatsapp_url"]
    assert "6588889999" not in saved["whatsapp_url"]
    assert saved["follow_up_at"] == ""


def test_business_onboarding_readiness_updates_after_first_enquiry():
    suffix = uuid.uuid4().hex[:8]
    slug = f"readiness-{suffix}"
    create = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Readiness Merchant",
            "business_type": "renovation",
            "whatsapp_phone": "6591110000",
            "contact_email": f"owner-{suffix}@example.com",
            "offer_summary": "Renovation quotation and site visit booking.",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert create.status_code == 200
    business_key = create.json()["business_access_key"]

    admin_profiles = client.get(
        "/apps/enquiry/api/business-profiles",
        headers={"X-Admin-Key": "test-admin"},
    )
    profile = next(item for item in admin_profiles.json()["profiles"] if item["slug"] == slug)
    assert profile["onboarding"]["status"] == "ready_for_test"
    assert profile["onboarding"]["percent"] >= 60
    assert "first_enquiry" in profile["onboarding"]["missing_keys"]

    listing_before = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        headers={"X-Business-Key": business_key},
    )
    assert listing_before.status_code == 200
    assert listing_before.json()["onboarding"]["status"] == "ready_for_test"
    assert listing_before.json()["onboarding"]["next_action"]

    lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Ready Buyer",
            "phone": "6512345678",
            "message": "I need a renovation quotation this week.",
            "pdpa_consent": True,
        },
    )
    assert lead.status_code == 200

    listing_after = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}",
        headers={"X-Business-Key": business_key},
    )
    onboarding = listing_after.json()["onboarding"]
    assert onboarding["status"] == "live"
    assert onboarding["percent"] == 100
    assert onboarding["missing_keys"] == []
    assert any(item["key"] == "first_enquiry" and item["done"] for item in onboarding["checks"])


def test_admin_merchant_health_flags_due_followups():
    suffix = uuid.uuid4().hex[:8]
    slug = f"health-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Health Merchant",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
            "contact_email": f"health-{suffix}@example.com",
            "offer_summary": "Repair quotation and appointment follow-up.",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Health Buyer",
            "phone": "6512345678",
            "message": "Need a repair quote urgently.",
            "pdpa_consent": True,
        },
    )
    assert lead.status_code == 200
    assert client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{lead.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2000-01-01", "internal_note": "Customer asked for callback."},
        headers={"X-Business-Key": business_key},
    ).status_code == 200

    unauthorized = client.get("/admin/merchant-health")
    assert unauthorized.status_code == 401

    health = client.get(
        f"/admin/merchant-health?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert health.status_code == 200
    merchant = next(item for item in health.json()["merchants"] if item["business_slug"] == slug)
    assert merchant["risk_level"] == "high"
    assert merchant["due_followups"] == 1
    assert merchant["onboarding_status"] == "live"
    assert health.json()["summary"]["due_followups"] >= 1


def test_merchant_share_links_are_tracked_and_business_scoped():
    suffix = uuid.uuid4().hex[:8]
    slug_one = f"share-one-{suffix}"
    slug_two = f"share-two-{suffix}"
    profile_one = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_one,
            "business_name": "Share One",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    profile_two = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug_two,
            "business_name": "Share Two",
            "business_type": "beauty",
            "whatsapp_phone": "6592220000",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile_one.status_code == 200
    assert profile_two.status_code == 200
    business_key = profile_one.json()["business_access_key"]

    missing_key = client.get(
        f"/apps/enquiry/api/merchant/share-links?business_slug={slug_one}&campaign=fb-june"
    )
    assert missing_key.status_code == 401

    blocked = client.get(
        f"/apps/enquiry/api/merchant/share-links?business_slug={slug_two}&campaign=fb-june",
        headers={"X-Business-Key": business_key},
    )
    assert blocked.status_code == 403

    response = client.get(
        f"/apps/enquiry/api/merchant/share-links?business_slug={slug_one}&campaign=fb-june",
        headers={"X-Business-Key": business_key},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["business_slug"] == slug_one
    assert body["campaign"] == "fb-june"
    assert "/embed/enquiry/" in body["embed_code"]
    assert slug_one in body["embed_code"]
    assert "source=facebook" in body["links"]["facebook"]["url"]
    assert "campaign=fb-june" in body["links"]["facebook"]["url"]
    assert "source=instagram" in body["links"]["instagram"]["url"]
    assert "source=whatsapp" in body["links"]["whatsapp"]["url"]
    assert "source=google-business" in body["links"]["google_business"]["url"]
    assert body["copy"]["whatsapp_text"].endswith(body["links"]["whatsapp"]["url"])
    assert profile_one.json()["business_access_key"] not in json.dumps(body)


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
    assert "auto_summary" not in exported.text
    assert "next_action" not in exported.text
    assert "follow_up_recommendation" not in exported.text
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
            "source": "instagram",
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
            "source": "tiktok",
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

    source_filtered = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=instagram",
        headers={"X-Business-Key": business_key},
    )
    assert source_filtered.status_code == 200
    source_names = [item["name"] for item in source_filtered.json()["enquiries"]]
    assert "Hot Quote Buyer" in source_names
    assert "Booking Buyer" not in source_names

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
        f"/apps/enquiry/api/merchant/enquiries/export.csv?business_slug={slug}&priority=hot&source=instagram&search=urgent",
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
    no_follow_update = client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{no_follow.json()['id']}?business_slug={slug}",
        json={"follow_up_at": ""},
        headers={"X-Business-Key": business_key},
    )
    assert overdue_update.status_code == 200
    assert future_update.status_code == 200
    assert no_follow_update.status_code == 200

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


def test_admin_backend_automation_runner_previews_operational_tasks():
    suffix = uuid.uuid4().hex[:8]
    slug = f"automation-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Automation Service",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
            "contact_email": f"automation-{suffix}@example.com",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    due_lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Automation Due Buyer",
            "phone": "6591112222",
            "message": "Need quotation this week",
            "pdpa_consent": True,
        },
    )
    assert due_lead.status_code == 200
    assert client.patch(
        f"/apps/enquiry/api/merchant/enquiries/{due_lead.json()['id']}?business_slug={slug}",
        json={"follow_up_at": "2000-01-01", "internal_note": "Automation preview note."},
        headers={"X-Business-Key": business_key},
    ).status_code == 200

    trial = client.post(
        "/apps/enquiry/api/trial-requests",
        json={
            "business_name": f"Automation Trial {suffix}",
            "contact_name": "Trial Owner",
            "contact_email": f"automation-trial-{suffix}@example.com",
            "whatsapp_phone": "6599998888",
            "business_type": "cleaning",
            "city": "Singapore",
            "monthly_enquiries": "10-30",
            "lead_source": "test",
            "message": "We need follow-up automation.",
            "pdpa_consent": True,
        },
    )
    assert trial.status_code == 200

    unauthorized = client.post("/admin/automation/run")
    assert unauthorized.status_code == 401

    response = client.post(
        "/admin/automation/run?dry_run=true&include_backup=true",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dry_run"] is True
    assert body["include_backup"] is True
    assert "deployment_checks" in body["tasks"]
    assert "trial_requests" in body["tasks"]
    assert "merchant_health" in body["tasks"]
    assert "followup_digest" in body["tasks"]
    assert "data_retention" in body["tasks"]
    assert "backup" in body["tasks"]
    assert body["tasks"]["backup"]["status"] == "dry_run"
    assert body["tasks"]["followup_digest"]["dry_run"] is True
    assert body["tasks"]["followup_digest"]["sent"] >= 1
    assert body["tasks"]["data_retention"]["dry_run"] is True
    assert body["tasks"]["merchant_health"]["summary"]["total"] >= 1
    assert body["tasks"]["merchant_health"]["summary"]["due_followups"] >= 1
    assert any(
        result.get("business_slug") == slug and result.get("due_count") == 1
        for result in body["tasks"]["followup_digest"]["results"]
    )
    assert body["tasks"]["trial_requests"]["stats"]["total"] >= 1
    assert body["next_step"]


def test_admin_data_retention_cleanup_previews_and_deletes_expired_enquiries():
    suffix = uuid.uuid4().hex[:8]
    slug = f"retention-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Retention Service",
            "business_type": "repair",
            "whatsapp_phone": "6591110000",
            "data_retention_days": 30,
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200

    lead = client.post(
        "/apps/enquiry/api/enquiries",
        json={
            "business_slug": slug,
            "name": "Expired Buyer",
            "phone": "6590001111",
            "message": "Need old repair quote",
            "pdpa_consent": True,
        },
    )
    assert lead.status_code == 200
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()
    with main.db_connection() as connection:
        connection.execute(
            "UPDATE enquiries SET created_at = ?, updated_at = ? WHERE id = ?",
            (old_timestamp, old_timestamp, lead.json()["id"]),
        )

    unauthorized = client.post(f"/admin/data-retention/cleanup?business_slug={slug}")
    assert unauthorized.status_code == 401

    preview = client.post(
        f"/admin/data-retention/cleanup?business_slug={slug}&dry_run=true",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert preview.status_code == 200
    assert preview.json()["expired"] == 1
    assert preview.json()["deleted"] == 0

    still_listed = client.get(
        f"/apps/enquiry/api/enquiries?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert any(item["id"] == lead.json()["id"] for item in still_listed.json()["enquiries"])

    cleaned = client.post(
        f"/admin/data-retention/cleanup?business_slug={slug}&dry_run=false",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert cleaned.status_code == 200
    assert cleaned.json()["expired"] == 1
    assert cleaned.json()["deleted"] == 1

    after = client.get(
        f"/apps/enquiry/api/enquiries?business_slug={slug}",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert all(item["id"] != lead.json()["id"] for item in after.json()["enquiries"])

    audit = client.get(
        f"/admin/data-audit-events?business_slug={slug}&event_type=enquiry.retention_deleted",
        headers={"X-Admin-Key": "test-admin"},
    )
    assert audit.status_code == 200
    assert audit.json()["events"][0]["metadata"]["count"] == 1
    audit_payload = json.dumps(audit.json()["events"])
    assert "Expired Buyer" not in audit_payload
    assert "6590001111" not in audit_payload


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


def meta_signature(payload, secret="test-meta-app-secret"):
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), "sha256").hexdigest()
    return f"sha256={digest}"


def test_meta_webhook_verify_and_whatsapp_message_creates_enquiry_once():
    verify = client.get(
        "/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "test-meta-verify-token",
            "hub.challenge": "challenge-123",
        },
    )
    assert verify.status_code == 200
    assert verify.text == "challenge-123"

    bad_verify = client.get(
        "/webhooks/meta",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "challenge-123",
        },
    )
    assert bad_verify.status_code == 403

    suffix = uuid.uuid4().hex[:8]
    slug = f"meta-wa-{suffix}"
    phone_number_id = f"phone-number-id-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Meta WhatsApp Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
            "offer_summary": "used car sales and loan support",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    connection = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/whatsapp?business_slug={slug}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "Dealer WABA",
            "external_account_id": phone_number_id,
            "data_processing_acknowledged": True,
            "notes": "Official Cloud API webhook",
        },
        headers={"X-Business-Key": business_key},
    )
    assert connection.status_code == 200

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-1",
                "changes": [
                    {
                        "value": {
                            "metadata": {"phone_number_id": phone_number_id},
                            "contacts": [{"wa_id": "60123456789", "profile": {"name": "Alex Buyer"}}],
                            "messages": [
                                {
                                        "id": f"wamid.test-{suffix}",
                                    "from": "60123456789",
                                    "timestamp": "1718000000",
                                    "type": "text",
                                    "text": {"body": "Can loan? monthly below RM900, can view today?"},
                                }
                            ],
                        }
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    bad_post = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": "sha256=bad", "Content-Type": "application/json"},
    )
    assert bad_post.status_code == 401

    first = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    assert first.json()["created"] == 1
    assert first.json()["duplicates"] == 0

    second = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] == 1

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=whatsapp",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if "RM900" in item["message"])
    assert saved["name"] == "Alex Buyer"
    assert saved["phone"] == "60123456789"
    assert saved["source"] == "whatsapp"
    assert saved["whatsapp_url"].startswith("https://wa.me/60123456789")
    assert saved["stuck_point"] == "Monthly payment or loan readiness"
    assert "official Meta channel" in saved["consent_notice"]


def test_meta_webhook_facebook_message_does_not_create_whatsapp_link():
    suffix = uuid.uuid4().hex[:8]
    slug = f"meta-fb-{suffix}"
    page_id = f"page-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Meta Facebook Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    connection = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/facebook?business_slug={slug}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "Dealer Facebook Page",
            "external_account_id": page_id,
            "data_processing_acknowledged": True,
            "notes": "Official Messenger webhook",
        },
        headers={"X-Business-Key": business_key},
    )
    assert connection.status_code == 200

    payload = {
        "object": "page",
        "entry": [
            {
                "id": page_id,
                "time": 1718000000000,
                "messaging": [
                    {
                        "sender": {"id": "psid-456"},
                            "recipient": {"id": page_id},
                        "timestamp": 1718000000000,
                        "message": {
                            "mid": f"m-facebook-{suffix}",
                            "text": "Is the Civic still available? Can view tomorrow?",
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    response = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["created"] == 1

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=facebook",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if "Civic" in item["message"])
    assert saved["source"] == "facebook"
    assert saved["phone"].startswith("facebook:")
    assert not saved["whatsapp_url"]
    assert saved["stuck_point"] == "Viewing appointment not confirmed"


def test_meta_webhook_instagram_message_creates_enquiry_without_whatsapp_link():
    suffix = uuid.uuid4().hex[:8]
    slug = f"meta-ig-{suffix}"
    instagram_account_id = f"ig-{suffix}"
    profile = client.post(
        "/apps/enquiry/api/business-profiles",
        json={
            "slug": slug,
            "business_name": "Meta Instagram Dealer",
            "business_type": "used_car_dealer",
            "whatsapp_phone": "6011112222",
        },
        headers={"X-Admin-Key": "test-admin"},
    )
    assert profile.status_code == 200
    business_key = profile.json()["business_access_key"]

    connection = client.patch(
        f"/apps/enquiry/api/merchant/channel-connections/instagram?business_slug={slug}",
        json={
            "integration_mode": "official_api_requested",
            "status": "requested",
            "account_label": "Dealer Instagram",
            "external_account_id": instagram_account_id,
            "data_processing_acknowledged": True,
            "notes": "Official Instagram messaging webhook",
        },
        headers={"X-Business-Key": business_key},
    )
    assert connection.status_code == 200

    payload = {
        "object": "instagram",
        "entry": [
            {
                "id": instagram_account_id,
                "time": 1718000000000,
                "messaging": [
                    {
                        "sender": {"id": "igid-789"},
                        "recipient": {"id": instagram_account_id},
                        "timestamp": 1718000000000,
                        "message": {
                            "mid": f"m-instagram-{suffix}",
                            "text": "Still got Vios? Need low deposit, can check loan?",
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    first = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert first.status_code == 200
    assert first.json()["created"] == 1

    second = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert second.status_code == 200
    assert second.json()["created"] == 0
    assert second.json()["duplicates"] == 1

    listing = client.get(
        f"/apps/enquiry/api/merchant/enquiries?business_slug={slug}&source=instagram",
        headers={"X-Business-Key": business_key},
    )
    assert listing.status_code == 200
    saved = next(item for item in listing.json()["enquiries"] if "Vios" in item["message"])
    assert saved["source"] == "instagram"
    assert saved["phone"].startswith("instagram:")
    assert not saved["whatsapp_url"]
    assert saved["stuck_point"] == "Monthly payment or loan readiness"


def test_meta_webhook_unmapped_response_masks_account_id():
    suffix = uuid.uuid4().hex[:8]
    raw_account_id = f"unmapped-page-account-{suffix}"
    payload = {
        "object": "page",
        "entry": [
            {
                "id": raw_account_id,
                "messaging": [
                    {
                        "sender": {"id": "psid-unmapped"},
                        "recipient": {"id": raw_account_id},
                        "timestamp": 1718000000000,
                        "message": {
                            "mid": f"m-unmapped-{suffix}",
                            "text": "Is this still available?",
                        },
                    }
                ],
            }
        ],
    }
    raw = json.dumps(payload, separators=(",", ":"))
    response = client.post(
        "/webhooks/meta",
        content=raw,
        headers={"X-Hub-Signature-256": meta_signature(raw), "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] == 0
    assert body["unmapped"] == 1
    assert body["items"][0]["account_id_preview"].startswith("unm...")
    assert raw_account_id not in json.dumps(body)


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
