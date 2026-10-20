/**
 * Literal hex values for the two places CSS custom properties cannot reach:
 * ECharts series colours and el-progress SVG strokes. Everywhere else must
 * read the CSS variables in styles/tokens.css.
 *
 * KEEP IN SYNC with styles/tokens.css -- 改色两处同步. A colour changed in one
 * file and not the other is exactly how the chart palette drifts away from the
 * rest of the UI while every test still passes. The values below are copied
 * from the token scale, not a second opinion on what they should be.
 */
export const accent = '#3e63dd'
export const accentHover = '#324fb1'
export const accentActive = '#2a4497'
export const positive = '#2e7d5b'
export const warning = '#b45309'
export const negative = '#b42318'
export const neutral = '#6f7681'

/**
 * Categorical chart palette: one hue, eight lightness steps. A greyscale pie
 * makes adjacent slices indistinguishable; eight unrelated hues is the rainbow
 * this redesign exists to remove. Darker = larger slice in a funnel.
 */
export const accentSeries = [
  '#2a4497', '#324fb1', '#3e63dd', '#5f7ce3',
  '#8094e9', '#a1adef', '#c2c7f5', '#e3e3fb',
]

/** Trend lines: four steps spread across the same ramp, not four hues. */
export const trendSeries = ['#2a4497', '#3e63dd', '#8094e9', '#c2c7f5']

/** Soft end of the ramp, for gradients and area fills. */
export const accentSoft = '#d4d9fa'
