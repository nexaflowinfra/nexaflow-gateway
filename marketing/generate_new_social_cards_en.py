from __future__ import annotations

from generate_new_social_cards import (
    F_BODY,
    F_BODY_B,
    F_CHIP,
    F_H1,
    F_H1_SMALL,
    F_LOGO,
    F_SMALL,
    GOLD,
    H,
    INK,
    LINE,
    MUTED,
    OUT_DIR,
    PANEL,
    TEAL,
    W,
    brand,
    button,
    chip,
    draw_wrapped,
    gradient_background,
    rounded_panel,
    text_width,
)
from PIL import ImageDraw


def card_trial_en():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (742, 56, 1018, 114), "30-day trial", fill=(24, 18, 6), outline=(96, 75, 30), text_fill=GOLD)

    d.text((64, 184), "30-day free trial", font=F_H1, fill=INK)
    d.text((64, 274), "AI WhatsApp assistant", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(
        d,
        (68, 364),
        "One link collects enquiries, organizes leads, and helps merchants follow up on WhatsApp.",
        F_BODY,
        MUTED,
        900,
        10,
    )

    rounded_panel(d, (64, 520, 1016, 804), radius=28, fill=(8, 12, 12), outline=(45, 61, 56))
    items = [
        ("01", "Customer sends enquiry", "Name, phone, service need, and data consent."),
        ("02", "AI organizes the lead", "Detects quote, appointment, general enquiry, and priority."),
        ("03", "Merchant follows up faster", "View it in a private inbox, then reply on WhatsApp."),
    ]
    y = 554
    for num, title, body in items:
        d.ellipse((92, y + 2, 144, y + 54), fill=GOLD)
        d.text((107, y + 14), num, font=F_SMALL, fill=(0, 0, 0))
        d.text((166, y), title, font=F_BODY_B, fill=INK)
        d.text((166, y + 40), body, font=F_SMALL, fill=MUTED)
        y += 82

    button(d, (64, 870, 430, 940), "Apply for trial")
    d.text((458, 886), "Built for renovation, beauty, repair, cleaning, tuition.", font=F_SMALL, fill=MUTED)
    img.save(OUT_DIR / "nexaflow-social-trial-30days-en.png", quality=95)


def card_no_website_en():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (760, 56, 1018, 114), "No website needed", fill=(7, 22, 21), outline=(28, 92, 88), text_fill=TEAL)

    d.text((64, 178), "No website?", font=F_H1, fill=INK)
    d.text((64, 268), "You can still collect enquiries.", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(
        d,
        (68, 362),
        "Share one enquiry link on WhatsApp, Facebook, Instagram, or Google Business Profile.",
        F_BODY,
        MUTED,
        920,
        10,
    )

    rounded_panel(d, (70, 525, 1010, 742), radius=32, fill=(8, 12, 12), outline=(45, 61, 56))
    x_positions = [138, 368, 598, 828]
    labels = ["WhatsApp", "Facebook", "Instagram", "Link"]
    colors = [TEAL, (90, 132, 255), (245, 116, 178), GOLD]
    for i, (x, label, col) in enumerate(zip(x_positions, labels, colors)):
        d.rounded_rectangle((x - 78, 575, x + 78, 660), radius=22, fill=(13, 17, 17), outline=col, width=2)
        d.text((x - text_width(d, label, F_SMALL) / 2, 604), label, font=F_SMALL, fill=INK)
        if i < 3:
            start = x + 88
            end = x_positions[i + 1] - 88
            d.line((start, 617, end, 617), fill=(92, 98, 94), width=2)
            d.polygon([(end, 617), (end - 16, 607), (end - 16, 627)], fill=(92, 98, 94))

    rounded_panel(d, (166, 790, 914, 912), radius=28, fill=(11, 15, 15), outline=(78, 68, 40))
    d.text((206, 820), "Customer opens link -> submits enquiry", font=F_BODY_B, fill=INK)
    d.text((206, 865), "Merchant receives it in a private inbox and follows up.", font=F_SMALL, fill=MUTED)
    img.save(OUT_DIR / "nexaflow-social-no-website-needed-en.png", quality=95)


def card_followup_en():
    img = gradient_background()
    d = ImageDraw.Draw(img)
    brand(d)
    chip(d, (780, 56, 1018, 114), "Follow-up", fill=(24, 18, 6), outline=(96, 75, 30), text_fill=GOLD)

    d.text((64, 178), "Customers ask.", font=F_H1, fill=INK)
    d.text((64, 268), "Do not miss the follow-up.", font=F_H1_SMALL, fill=GOLD)
    draw_wrapped(
        d,
        (68, 362),
        "Bring WhatsApp, Facebook, and Instagram enquiries into one private inbox so merchants know who to reply to.",
        F_BODY,
        MUTED,
        900,
        10,
    )

    rounded_panel(d, (64, 512, 1016, 835), radius=30, fill=(8, 12, 12), outline=(45, 61, 56))
    stages = [
        ("New lead", "Customer request captured", TEAL),
        ("AI organized", "Intent + priority detected", GOLD),
        ("WhatsApp follow-up", "Reply faster with context", (255, 255, 255)),
    ]
    y = 560
    for idx, (title, body, col) in enumerate(stages, start=1):
        d.rounded_rectangle((104, y, 198, y + 58), radius=18, fill=(14, 18, 18), outline=col, width=2)
        d.text((134, y + 12), f"{idx}", font=F_BODY_B, fill=col)
        d.text((230, y - 2), title, font=F_BODY_B, fill=INK)
        d.text((230, y + 39), body, font=F_SMALL, fill=MUTED)
        if idx < len(stages):
            d.line((151, y + 72, 151, y + 94), fill=(78, 86, 82), width=3)
        y += 94

    button(d, (64, 890, 412, 960), "DM for trial")
    draw_wrapped(d, (442, 898), "For WhatsApp-based local service businesses.", F_SMALL, MUTED, 560, 6)
    img.save(OUT_DIR / "nexaflow-social-followup-inbox-en.png", quality=95)


def main():
    card_trial_en()
    card_no_website_en()
    card_followup_en()
    print("Generated:")
    print(OUT_DIR / "nexaflow-social-trial-30days-en.png")
    print(OUT_DIR / "nexaflow-social-no-website-needed-en.png")
    print(OUT_DIR / "nexaflow-social-followup-inbox-en.png")


if __name__ == "__main__":
    main()
