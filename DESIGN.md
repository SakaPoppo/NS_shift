
## Brand & Style

This design system is built on the principles of **Clinical Minimalism** and **High-Affordance Functionalism**. It is designed specifically for the high-pressure medical and professional environment where clarity, speed of cognition, and physical ease of use are paramount.

The brand personality is authoritative yet approachable—it acts as a calm, silent partner in administrative tasks. By utilizing heavy whitespace and a restricted color palette, we reduce cognitive load for users who may be fatigued. The emotional response should be one of "controlled efficiency"—a feeling that the software is organized, reliable, and error-resistant. 

The aesthetic leans into **Corporate Modern** with a focus on institutional reliability. It avoids decorative flourishes in favor of structural clarity, ensuring that shift management feels like a seamless, rhythmic process rather than a complex chore.

## Colors

The color strategy prioritizes legibility and semantic meaning. 
- **Primary Blue (#2563EB):** Used for primary actions and brand presence. It evokes trust and clinical professionalism.
- **Surface Tint (#EFF6FF):** A low-contrast secondary blue used for large background areas and secondary containers to soften the interface compared to pure white.
- **Functional Accents:** Warning (Orange), Success (Green), and Error (Red) colors are used sparingly and only to denote status, ensuring they remain highly effective when they appear.
- **Neutral Foundation:** The background (#F8FAFC) provides a crisp, cool-toned canvas that differentiates clearly from white card elements.

## Typography

We use **Public Sans** for its institutional clarity and high readability in data-heavy environments. The typographic scale is generous, favoring larger body sizes to ensure accessibility for all staff members.

- **Headlines:** Use Bold weights with slight negative letter-spacing to create a strong visual anchor for page sections.
- **Body:** Standardized at 16px or 18px to ensure comfort during long periods of shift planning.
- **Labels:** Semibold weights are used for form labels to ensure they remain distinct from the user's input data.
- **Mobile scaling:** Headlines scale down significantly on mobile to prevent awkward text wrapping in narrow shift table views.

## Layout & Spacing

The layout follows a **Fixed Grid** philosophy on desktop (max-width: 1280px) to maintain a consistent scan-line for administrators. On mobile, it transitions to a fluid single-column layout.

- **Rhythm:** A strict 8px base unit (1rem = 16px) governs all spacing. 
- **White Space:** Large gaps (stack-gap-lg) are used between major functional blocks to prevent the UI from feeling "crowded," which is a common complaint in medical software.
- **Touch Targets:** All interactive elements must maintain a minimum height of 48px to accommodate rapid touch interactions on tablets and mobile devices.

## Elevation & Depth

This design system uses **Tonal Layering** combined with **Ambient Shadows** to create a clear hierarchy of information.

- **Floor:** The background (#F8FAFC) acts as the furthest depth layer.
- **Surface:** Main content cards use a pure White (#FFFFFF) background.
- **Shadows:** We use a single, highly diffused "Soft Focus" shadow for cards.
  - *Offset: 0px 4px | Blur: 20px | Color: #0F172A at 5% opacity.*
- **Interactive Depth:** On hover, buttons and cards may slightly increase their shadow spread to provide tactile feedback, but we avoid heavy lifts to maintain a clean, professional aesthetic.

## Shapes

The shape language is defined by **Friendly Geometry**. By using the `Rounded` setting, we remove the "sharpness" often associated with clinical software, making the tool feel more modern and user-friendly.

- **Cards:** Use `rounded-xl` (1.5rem / 24px) to create a soft, contained look for shift modules.
- **Inputs & Buttons:** Use `rounded-md` (0.5rem / 8px) to provide a clear, professional boundary that feels intentional and sturdy.
- **Chips/Badges:** Are fully rounded (pill-shaped) to distinguish them from interactive buttons.

## Components

### Buttons
- **Primary:** #2563EB background, White text. Minimum 48px height. Bold, centered text.
- **Secondary:** White background, 2px border in #2563EB, Blue text. Used for "Cancel" or "Add Optional" actions.
- **Ghost:** No background or border, Blue text. Used for low-priority navigation in the header.

### Form Fields
- **Structure:** Vertical stack. Label (Label-md, Text-primary) -> Helper Text (Label-sm, Slate-500) -> Input Field.
- **Inputs:** 48px height, 1px border (#CBD5E1), 12px horizontal padding. Focus state uses a 2px Primary Blue border.

### Cards
- White background, `rounded-xl`, soft ambient shadow. 
- Padding should be generous (24px or 32px) to frame content clearly.

### Header
- Height: 72px.
- Background: White with a subtle bottom border (1px, #E2E8F0).
- Logo: Left-aligned, Primary Blue. 
- Navigation: Right-aligned using Ghost buttons with 16px spacing between items.

### Shift Blocks (Specific Component)
- Used within the calendar view. Rounded corners (8px), using Primary Blue for occupied shifts and Surface Tint (#EFF6FF) for open slots. 
- Text inside shift blocks should use `label-sm`.