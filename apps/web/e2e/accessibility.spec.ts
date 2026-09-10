import { expect, item, lowConfidence, stubApi, test, undated } from './fixtures';

/**
 * Accessibility rules as executable checks.
 *
 * These exist because contrast and target-size regressions are invisible in
 * code review and only surface when somebody cannot use the product. One of
 * them already caught a real bug during development: the heatmap's week labels
 * used secondary text, which falls to 2.5:1 against the darker steps of the
 * load ramp.
 *
 * Thresholds come from the HIG accessibility table: 4.5:1 for text up to 17pt,
 * 3:1 at 18pt or bold, 44pt touch targets and 24pt pointer targets.
 */

const CONTRAST_SCRIPT = `
  (() => {
    const parse = (value) => {
      const m = value.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)(?:,\\s*([\\d.]+))?/);
      return m ? { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] } : null;
    };
    const lum = ({ r, g, b }) => {
      const f = (c) => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
      return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
    };
    const ratio = (a, b) => {
      const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x);
      return (hi + 0.05) / (lo + 0.05);
    };
    // Walk up for the nearest painted ancestor: a transparent background means
    // the text actually sits on whatever is behind it.
    const backdrop = (el) => {
      let node = el;
      while (node && node !== document.documentElement) {
        const bg = parse(getComputedStyle(node).backgroundColor);
        if (bg && bg.a > 0.95) return bg;
        node = node.parentElement;
      }
      return { r: 255, g: 255, b: 255, a: 1 };
    };

    const failures = [];
    for (const el of document.querySelectorAll('*')) {
      if (!el.childNodes.length) continue;
      // Only elements that render their own text.
      const own = [...el.childNodes].some((n) => n.nodeType === 3 && n.textContent.trim().length > 1);
      if (!own) continue;

      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none' || +cs.opacity === 0) continue;
      const rect = el.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) continue;

      const fg = parse(cs.color);
      if (!fg || fg.a < 0.95) continue;

      const size = parseFloat(cs.fontSize);
      const bold = +cs.fontWeight >= 700;
      const required = size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;

      const value = ratio(fg, backdrop(el));
      if (value < required) {
        failures.push({
          text: el.textContent.trim().slice(0, 45),
          selector: el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''),
          color: cs.color,
          size,
          ratio: Math.round(value * 100) / 100,
          required,
        });
      }
    }
    return failures;
  })()
`;

const TARGET_SCRIPT = `
  (() => {
    const min = window.matchMedia('(pointer: coarse)').matches ? 44 : 24;

    // What the user actually has to hit. A checkbox wrapped in a label is
    // activated by clicking anywhere in the label, so the label is the target;
    // measuring the 18px box alone would report a failure that is not real.
    const effective = (el) => {
      const label = el.closest('label');
      return label ? label.getBoundingClientRect() : el.getBoundingClientRect();
    };

    return [...document.querySelectorAll('button, a[href], input, select, [role=button]')]
      .filter((el) => {
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none') return false;
        // A visually hidden input paired with a styled label is a standard
        // pattern; the label carries the hit area and is measured instead.
        if (el.classList.contains('visually-hidden') && el.closest('label')) return false;
        const r = effective(el);
        if (r.width === 0 || r.height === 0) return false;
        return r.height < min || r.width < min;
      })
      .map((el) => {
        const r = effective(el);
        return {
          selector: el.tagName.toLowerCase() + (el.className ? '.' + String(el.className).split(' ')[0] : ''),
          label: (el.getAttribute('aria-label') || el.textContent || '').trim().slice(0, 30),
          width: Math.round(r.width),
          height: Math.round(r.height),
          min,
        };
      });
  })()
`;

const SCREENS = [
  { name: 'timeline', path: '/timeline' },
  { name: 'review', path: '/review/d-1' },
  { name: 'upload', path: '/upload' },
  { name: 'search', path: '/search' },
];

test.describe('accessibility', () => {
  for (const screen of SCREENS) {
    test(`${screen.name} meets contrast minimums`, async ({ page }) => {
      await stubApi(page, { timeline: [item(), lowConfidence(), undated()] });
      await page.goto(screen.path);
      await page.waitForLoadState('networkidle');

      const failures = await page.evaluate(CONTRAST_SCRIPT);
      expect(failures, `low-contrast text on ${screen.name}`).toEqual([]);
    });

    test(`${screen.name} meets target size minimums`, async ({ page }) => {
      await stubApi(page, { timeline: [item(), lowConfidence(), undated()] });
      await page.goto(screen.path);
      await page.waitForLoadState('networkidle');

      const small = await page.evaluate(TARGET_SCRIPT);
      expect(small, `undersized targets on ${screen.name}`).toEqual([]);
    });
  }

  test('the heatmap ramp keeps its labels readable at every step', async ({ page }) => {
    // The specific regression this guards: cells sit on the load ramp rather
    // than on the page background, so a text color chosen against the page
    // can fail badly against the darkest steps.
    await stubApi(page, {
      timeline: [item()],
      heatmap: Array.from({ length: 16 }, (_, i) => ({
        week_start: `2026-08-${String(24 + i).padStart(2, '0')}`,
        effort_hours: 4 + i * 2,
        flags: [],
      })),
    });
    await page.goto('/timeline');
    await page.waitForLoadState('networkidle');

    const failures = await page.evaluate(CONTRAST_SCRIPT);
    const inCells = (failures as { selector: string }[]).filter((f) =>
      f.selector.startsWith('span.cell'),
    );
    expect(inCells).toEqual([]);
  });

  test('every interactive control has an accessible name', async ({ page }) => {
    await stubApi(page, { timeline: [item(), lowConfidence()] });
    await page.goto('/review/d-1');

    const unnamed = await page.evaluate(`
      [...document.querySelectorAll('button, a[href], [role=button]')]
        .filter((el) => !(el.getAttribute('aria-label') || el.textContent || '').trim())
        .map((el) => el.outerHTML.slice(0, 80))
    `);
    expect(unnamed).toEqual([]);
  });

  test('every input has a label', async ({ page }) => {
    await stubApi(page);
    await page.goto('/search');

    const unlabelled = await page.evaluate(`
      [...document.querySelectorAll('input, select, textarea')]
        .filter((el) => {
          if (el.getAttribute('aria-label')) return false;
          if (el.closest('label')) return false;
          return !(el.id && document.querySelector('label[for="' + el.id + '"]'));
        })
        .map((el) => el.outerHTML.slice(0, 80))
    `);
    expect(unlabelled).toEqual([]);
  });

  test('headings descend without skipping a level', async ({ page }) => {
    await stubApi(page, { timeline: [item(), lowConfidence(), undated()] });
    await page.goto('/review/d-1');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();

    const levels: number[] = await page.evaluate(`
      [...document.querySelectorAll('h1,h2,h3,h4,h5,h6')].map((h) => +h.tagName[1])
    `);
    expect(levels[0]).toBe(1);
    for (let i = 1; i < levels.length; i += 1) {
      expect(levels[i] - levels[i - 1], `heading jump at index ${i}`).toBeLessThanOrEqual(1);
    }
  });

  test('focus is visible on every control', async ({ page }) => {
    await stubApi(page, { timeline: [item()] });
    await page.goto('/timeline');

    const suppressed = await page.evaluate(`
      [...document.querySelectorAll('button, a[href], input, select')]
        .filter((el) => {
          const cs = getComputedStyle(el, ':focus-visible');
          return cs.outlineStyle === 'none' && cs.outlineWidth === '0px' && !cs.boxShadow.includes('rgb');
        }).length
    `);
    expect(suppressed).toBe(0);
  });

  test('the keyboard can reach the content past the navigation', async ({ page }) => {
    await stubApi(page, { timeline: [item()] });
    await page.goto('/timeline');

    // Wait for the app to have rendered before pressing a key. Tabbing into a
    // document that is still bootstrapping moves focus nowhere, and the
    // resulting failure looks like a missing skip link rather than a race.
    const skip = page.getByRole('link', { name: 'Skip to content' });
    await expect(skip).toBeAttached();
    await expect(page.getByRole('main')).toBeVisible();

    await page.keyboard.press('Tab');
    await expect(skip).toBeFocused();
  });

  test('no neon colors anywhere in the palette', async ({ page }) => {
    await stubApi(page, { timeline: [item(), lowConfidence()] });
    await page.goto('/review/d-1');

    // Saturated, bright colors would make an ordinary busy week read as an
    // emergency. The palette is muted on purpose and should stay that way.
    const neon = await page.evaluate(`
      (() => {
        const found = new Set();
        for (const el of document.querySelectorAll('*')) {
          if (!el.checkVisibility || !el.checkVisibility()) continue;
          const cs = getComputedStyle(el);
          for (const prop of ['color', 'backgroundColor', 'borderTopColor', 'borderLeftColor']) {
            const m = cs[prop].match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
            if (!m) continue;
            const [r, g, b] = [+m[1], +m[2], +m[3]];
            const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
            const sat = mx === 0 ? 0 : (mx - mn) / mx;
            if (sat > 0.85 && mx > 230) found.add(prop + ': ' + cs[prop]);
          }
        }
        return [...found];
      })()
    `);
    expect(neon).toEqual([]);
  });

  test('the interface renders in dark mode without losing contrast', async ({ page }) => {
    await page.emulateMedia({ colorScheme: 'dark' });
    await stubApi(page, { timeline: [item(), lowConfidence(), undated()] });
    await page.goto('/review/d-1');
    await page.waitForLoadState('networkidle');

    const failures = await page.evaluate(CONTRAST_SCRIPT);
    expect(failures, 'low-contrast text in dark mode').toEqual([]);
  });

  test('reduced motion is honored', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await stubApi(page, { timeline: [item()] });
    await page.goto('/review/d-1');

    const animated = await page.evaluate(`
      [...document.querySelectorAll('*')].filter((el) => {
        const cs = getComputedStyle(el);
        const d = parseFloat(cs.animationDuration) || 0;
        const t = parseFloat(cs.transitionDuration) || 0;
        return d > 0.05 || t > 0.05;
      }).length
    `);
    expect(animated).toBe(0);
  });
});
