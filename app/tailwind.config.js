/** @type {import('tailwindcss').Config} */
// Extracted from the former inline tailwind.config (index.html) so the CSS can be
// compiled to a static file at image-build time — no CDN JIT at runtime (F15).
// `content` scans the markup incl. the #_safelist div that lists JS-generated
// classes. The `forms` plugin replaces the CDN ?plugins=forms; container-queries
// is dropped (no @container classes exist in the markup).
module.exports = {
    darkMode: "class",
    theme: {
        extend: {
            "colors": {
                "on-tertiary-container": "#00311f",
                "on-primary-fixed": "#23005c",
                "surface": "#0b1326",
                "surface-variant": "#2d3449",
                "secondary-container": "#03b5d3",
                "error": "#ffb4ab",
                "tertiary": "#4edea3",
                "outline-variant": "#494454",
                "primary": "#d0bcff",
                "surface-container-low": "#131b2e",
                "error-container": "#93000a",
                "secondary-fixed": "#acedff",
                "tertiary-fixed-dim": "#4edea3",
                "inverse-primary": "#6d3bd7",
                "primary-fixed-dim": "#d0bcff",
                "surface-bright": "#31394d",
                "surface-container-lowest": "#060e20",
                "surface-tint": "#d0bcff",
                "on-secondary-container": "#00424e",
                "secondary-fixed-dim": "#4cd7f6",
                "on-error-container": "#ffdad6",
                "on-surface-variant": "#cbc3d7",
                "surface-container-highest": "#2d3449",
                "on-primary-container": "#340080",
                "tertiary-container": "#00a572",
                "on-tertiary-fixed-variant": "#005236",
                "surface-dim": "#0b1326",
                "on-secondary": "#003640",
                "inverse-surface": "#dae2fd",
                "on-secondary-fixed": "#001f26",
                "outline": "#958ea0",
                "tertiary-fixed": "#6ffbbe",
                "on-tertiary": "#003824",
                "on-primary": "#3c0091",
                "secondary": "#4cd7f6",
                "on-primary-fixed-variant": "#5516be",
                "on-secondary-fixed-variant": "#004e5c",
                "primary-fixed": "#e9ddff",
                "on-tertiary-fixed": "#002113",
                "surface-container": "#171f33",
                "on-background": "#dae2fd",
                "on-error": "#690005",
                "background": "#0b1326",
                "inverse-on-surface": "#283044",
                "primary-container": "#a078ff",
                "surface-container-high": "#222a3d",
                "on-surface": "#dae2fd"
            },
            "borderRadius": {
                "DEFAULT": "0.25rem",
                "lg": "0.5rem",
                "xl": "0.75rem",
                "full": "9999px"
            },
            "spacing": {
                "margin": "32px",
                "xl": "48px",
                "lg": "24px",
                "md": "16px",
                "sm": "8px",
                "unit": "4px",
                "xs": "4px",
                "gutter": "20px"
            },
            "fontFamily": {
                "label-sm": ["JetBrains Mono"],
                "headline-lg": ["Geist"],
                "body-sm": ["Geist"],
                "title-md": ["Geist"],
                "body-lg": ["Geist"],
                "headline-lg-mobile": ["Geist"],
                "display-lg": ["Geist"],
                "label-md": ["JetBrains Mono"]
            },
            "fontSize": {
                "label-sm": ["10px", {"lineHeight": "1.2", "fontWeight": "500"}],
                "headline-lg": ["32px", {"lineHeight": "1.2", "letterSpacing": "-0.01em", "fontWeight": "600"}],
                "body-sm": ["14px", {"lineHeight": "1.5", "fontWeight": "400"}],
                "title-md": ["20px", {"lineHeight": "1.4", "fontWeight": "500"}],
                "body-lg": ["16px", {"lineHeight": "1.6", "fontWeight": "400"}],
                "headline-lg-mobile": ["24px", {"lineHeight": "1.2", "fontWeight": "600"}],
                "display-lg": ["48px", {"lineHeight": "1.1", "letterSpacing": "-0.02em", "fontWeight": "700"}],
                "label-md": ["12px", {"lineHeight": "1.2", "letterSpacing": "0.05em", "fontWeight": "500"}]
            }
        }
    }
,
    content: ["./static/index.html"],
    plugins: [require('@tailwindcss/forms')],
}
