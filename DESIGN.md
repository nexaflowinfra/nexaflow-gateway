---
name: NexaFlow Enquiry
description: One private sales inbox for scattered customer enquiries and AI-guided follow-up.
colors:
  night: "#000000"
  night-gradient-top: "#070707"
  surface: "#0c0c0d"
  surface-raised: "#141312"
  surface-warm: "#191715"
  ink: "#f7f3ea"
  muted: "#aaa39a"
  line: "#ffffff18"
  soft-gold: "#18140d"
  gold: "#f3c76a"
  gold-strong: "#ffe3a0"
  teal: "#45d5c7"
  danger: "#ef4444"
typography:
  display:
    fontFamily: "Inter, Segoe UI, Arial, sans-serif"
    fontSize: "48px"
    fontWeight: 800
    lineHeight: 1.04
    letterSpacing: "0"
  display-mobile:
    fontFamily: "Inter, Segoe UI, Arial, sans-serif"
    fontSize: "32px"
    fontWeight: 800
    lineHeight: 1.04
    letterSpacing: "0"
  body:
    fontFamily: "Inter, Segoe UI, Arial, sans-serif"
    fontSize: "16px"
    fontWeight: 400
    lineHeight: 1.6
    letterSpacing: "0"
  label:
    fontFamily: "Inter, Segoe UI, Arial, sans-serif"
    fontSize: "13px"
    fontWeight: 800
    lineHeight: 1.2
    letterSpacing: "0"
rounded:
  sm: "8px"
  md: "10px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "18px"
  xl: "34px"
  hero: "44px"
components:
  button-primary:
    backgroundColor: "{colors.gold}"
    textColor: "{colors.night}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
    height: "40px"
  button-secondary:
    backgroundColor: "#ffffff06"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "10px 14px"
    height: "40px"
  card:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    padding: "18px"
  chip:
    backgroundColor: "{colors.night-gradient-top}"
    textColor: "{colors.muted}"
    rounded: "{rounded.pill}"
    padding: "4px 9px"
---

# Design System: NexaFlow Enquiry

## 1. Overview

**Creative North Star: "The Calm Sales Control Room"**

NexaFlow should look like a serious, private sales operating system: dark, quiet, premium, and immediately useful. The visual job is to help a busy merchant understand the workflow without reading much: scattered enquiries become a queue, AI highlights what matters, and the team follows up with confidence.

The system should feel more like business infrastructure than a campaign page. It uses warm black surfaces, rare gold emphasis, thin borders, compact cards, and direct workflow previews. Used car dealer demos may be specific, but the public product page must stay broad enough for service merchants, sales teams, and other enquiry-heavy businesses.

**Key Characteristics:**
- Warm-black base with restrained gold emphasis.
- Practical queue previews instead of abstract AI decoration.
- Compact controls and cards that work on mobile first.
- Security and data protection shown calmly, never as fear copy.
- Bilingual English / Chinese support where adoption needs it.

## 2. Colors

The palette is a warm black operating-room system with gold used as a rare decision color and teal reserved for healthy/connected states.

### Primary
- **Signal Gold** (`gold`): Used for the primary call to action, selected states, important next-action highlights, and the active glass toggle thumb.
- **Warm Gold Highlight** (`gold-strong`): Used for hover gradients, high-emphasis labels, and small moments where the system needs to feel premium.

### Secondary
- **Connected Teal** (`teal`): Reserved for success, completed setup, healthy sync, and connected-source status. It must not compete with gold as a main brand color.
- **Action Red** (`danger`): Reserved for destructive, failed, or security-risk states only.

### Neutral
- **Nexa Black** (`night`): The default page background.
- **Raised Ink Surface** (`surface`): The default card and inbox-panel surface.
- **Warm Panel Surface** (`surface-raised`, `surface-warm`): Used for headers, pricing panels, and elevated preview areas.
- **Soft Ink** (`ink`): Primary text on dark surfaces.
- **Muted Stone** (`muted`): Secondary text. Keep contrast readable, especially on mobile.
- **Hairline Glass** (`line`): Thin borders and dividers. Do not thicken into heavy boxes.

### Named Rules

**The Rare Gold Rule.** Gold is the decision color. Use it for the one thing the merchant should do next, not as decoration across every card.

**The Broad Merchant Rule.** The homepage palette and examples must not visually lock the brand to used cars. Dealer specificity belongs in `/dealer-demo`, not the main brand surface.

## 3. Typography

**Display Font:** Inter with Segoe UI / Arial fallback  
**Body Font:** Inter with Segoe UI / Arial fallback  
**Label Font:** Inter with Segoe UI / Arial fallback

**Character:** The current system uses one practical sans family for clarity and speed. It is acceptable for shipping continuity, but future larger brand refreshes should test a more distinctive business-infrastructure typeface instead of choosing Inter by reflex.

### Hierarchy
- **Display** (800, `48px`, `1.04`): Hero headlines and the main value proposition only. On mobile it steps down to `32px`.
- **Headline** (800, `24px`, compact): Section titles and panel titles.
- **Title** (800, `18px-19px`): Queue preview titles, lead names, and pricing card headings.
- **Body** (400, `16px`, `1.6`): Explanation copy, trust notes, and long bilingual passages.
- **Label** (800-900, `12px-13px`, uppercase only for short labels): Eyebrows, status chips, tags, and setup metadata.

### Named Rules

**The Merchant-Speed Rule.** If a merchant cannot scan the first viewport in five seconds, the type hierarchy is too busy.

**The No Tiny Gray Paragraph Rule.** Muted text must remain readable on mobile. Never use small gray copy as the only explanation of an important action.

## 4. Elevation

NexaFlow uses tonal layering first and shadows second. Surfaces are separated by thin borders, warm black panels, and restrained glow rather than heavy drop shadows. Shadows should make the product feel private and premium, not like floating SaaS cards.

### Shadow Vocabulary
- **Brand Mark Glow** (`0 0 18px rgba(243,199,106,.18)`): Used only for the NexaFlow mark.
- **Panel Depth** (`0 24px 70px rgba(0,0,0,.28)`): Used for product panels and form cards.
- **Preview Depth** (`0 28px 90px rgba(0,0,0,.34)`): Used for hero previews only.
- **Floating Contact Depth** (`0 16px 40px rgba(0,0,0,.38)`): Used for the fixed WhatsApp button.

### Named Rules

**The Flat-Until-Useful Rule.** Cards are flat by default. Add stronger depth only when the element is a primary preview, modal-like panel, or floating action.

## 5. Components

### Buttons
- **Shape:** Small confident corners (`8px`), not oversized soft pills except for contact/navigation chips.
- **Primary:** Signal Gold background (`#f3c76a`) with black text, `10px 14px` padding, and `40px` minimum height.
- **Hover / Focus:** Hover may brighten to a gold-white gradient. Focus needs a visible ring or border without changing layout.
- **Secondary:** Transparent dark surface with a thin hairline border. Use for lower-priority navigation such as demo, login, or secondary setup.

### Liquid Glass Toggles
- **Style:** Compact glass capsule with a sliding inner thumb, warm gold active state, and subtle blur/saturation.
- **State:** The thumb moves between two equal segments. The inactive label remains readable but visually quieter.
- **Mobile:** Controls must stay small and not dominate the hero. If they draw more attention than the headline, reduce width, padding, and font size.

### Chips
- **Style:** Pill shape (`999px`) with thin border and muted text.
- **State:** Gold chip means current priority or demo state. Teal chip means healthy/connected. Gray chip means metadata.

### Cards / Containers
- **Corner Style:** Small radius (`8px`) for most surfaces, `10px` only for larger visual panels.
- **Background:** Warm black surface with thin border. Accent cards may use a subtle gold/teal wash but must stay readable.
- **Shadow Strategy:** Use Panel Depth only on major product panels. Avoid nested card shadows.
- **Internal Padding:** `14px-18px` for compact panels; larger content blocks may use `18px`.

### Inputs / Fields
- **Style:** Dark input surface, thin border, small radius, readable placeholder.
- **Focus:** Border or glow should show focus clearly without jumping layout.
- **Error / Disabled:** Error copy should be plain text and specific. Never show raw objects such as `[object Object]`.

### Navigation
- **Style:** Sticky black header with blurred background, compact logo mark, one login link, and one WhatsApp action.
- **Mobile:** Hide nonessential nav links. Keep the WhatsApp button short enough that it does not push the brand off screen.

### Sales Queue Preview
- **Style:** The signature component. It should show a short list of enquiries, the selected customer, the stuck point, and the next reply.
- **Behavior:** On marketing pages it demonstrates the workflow; in demo pages it becomes interactive. Keep advanced setup panels collapsed until needed.

## 6. Do's and Don'ts

### Do:
- **Do** lead with the workflow: source, request, missing details, stuck point, next reply, reminder.
- **Do** keep the homepage broad for merchants and sales teams. Used car dealer language belongs in demo-specific surfaces.
- **Do** keep gold rare and directional. One clear primary action per area.
- **Do** keep mobile controls small, readable, and below the navigation without overpowering the hero.
- **Do** show security and data protection with short, calm language.
- **Do** preserve bilingual English / Chinese support on adoption-critical pages.

### Don't:
- **Don't** make the public homepage only for used car dealers.
- **Don't** make "one-click reply" the whole value proposition; NexaFlow is about personal-skill follow-up support, qualification, missing details, and reminders.
- **Don't** use generic AI SaaS purple-blue gradients, decorative card walls, or abstract AI blobs.
- **Don't** build a complex enterprise CRM first impression.
- **Don't** nest cards inside cards unless the inner card is a real repeated item or detail panel.
- **Don't** expose platform setup, webhook, token, or security jargon in the first impression unless the user asked for setup.
- **Don't** let text overflow buttons, status chips, mobile cards, or floating WhatsApp controls.
