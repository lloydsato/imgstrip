/* Which options apply to an image, and which cannot be done for it.
 *
 * Pure rules, no DOM: the page greys options out by them, and the tests run them in Node against the real engine.
 * Two kinds of "no":
 *   na           the option has nothing to act on for this image (a JPEG-only switch on a PNG): greyed out.
 *   unsupported  the option is meaningful for this image but cannot be done (the engine would refuse it, or
 *                skip it): greyed out with a yellow warning triangle and the reason.
 * `facts` is what the server read from the image header (null when it could not): with no facts, nothing is
 * ruled out. `prefs` are the options as the user set them. `env` carries the server's limits and capabilities.
 */
'use strict';

(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.ImgstripSupport = factory();
})(typeof self !== 'undefined' ? self : this, function () {
  const OK = 'ok';
  const NA = 'na';
  const UNSUPPORTED = 'unsupported';

  const LOSSY = ['JPEG', 'WEBP', 'AVIF', 'HEIF'];
  const NO_LSB_MODES = ['1', 'I', 'F'];            // no 8/16-bit samples whose lowest bit could be randomised
  const NO_FINGERPRINT_MODES = ['P', '1', 'I', 'F']; // palette, 1-bit and 32-bit images carry no usable sensor noise
  const ANIMATED_SOURCES = ['GIF', 'WEBP', 'AVIF'];  // formats whose frames are all kept when saved as PNG

  const OPTION_NAMES = [
    'quality', 'icc', 'deep', 'jpeg_lossless', 'scrub_lsb', 'scrub_alpha', 'scrub_dct', 'jpeg_canonical', 'scrub_prnu',
    'random_name', 'c2pa', 'auto_orient', 'optimize',
  ];
  // What an option is worth when it cannot apply: the engine's own default, so that sending it changes nothing
  // (turning orientation off would make it warn that a tag was discarded, even when the image has none).
  const OFF = {
    quality: '', icc: 'convert', deep: false, jpeg_lossless: false, scrub_lsb: false, scrub_alpha: false, scrub_dct: false,
    jpeg_canonical: false, scrub_prnu: false, random_name: false, c2pa: false, auto_orient: true, optimize: false,
  };

  const ok = () => ({ state: OK, reason: '' });
  const na = (reason) => ({ state: NA, reason });
  const no = (reason) => ({ state: UNSUPPORTED, reason });

  const nameOf = (f) => ({ WEBP: 'WebP', HEIF: 'HEIF' }[f] || f);
  const thousands = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  function humanBytes(n) {
    if (n >= 1 << 30) return `${+(n / 2 ** 30).toFixed(1)} GB`;
    if (n >= 1 << 20) return `${+(n / 2 ** 20).toFixed(1)} MB`;
    if (n >= 1 << 10) return `${+(n / 2 ** 10).toFixed(1)} KB`;
    return `${n} B`;
  }

  function outputFormat(facts, prefs) {
    return prefs.format || (facts && facts.default_output) || null;
  }

  // Which of the JPEG-only options are in play: they only run when a JPEG stays a JPEG.
  function jpegStays(facts, prefs) {
    return !!facts && facts.format === 'JPEG' && outputFormat(facts, prefs) === 'JPEG';
  }
  // The JPEG switches that work on the compressed data instead of the pixels, by the names the page shows them under.
  const JPEG_DATA_OPTIONS = {
    jpeg_lossless: 'Keep JPEG data bit-exact', scrub_dct: 'Scrub JPEG coefficients', jpeg_canonical: 'Rebuild JPEG data canonically',
  };
  const jpegDataOptions = (prefs) => Object.keys(JPEG_DATA_OPTIONS).filter((name) => prefs[name]).map((name) => JPEG_DATA_OPTIONS[name]);
  const jpegDataOn = (prefs) => jpegDataOptions(prefs).length > 0;
  const listed = (names) => (names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}` : names[0]);

  // What happens to the JPEG data instead of a re-encode, in the engine's order: a coefficient scrub wins over a
  // canonical rebuild, which wins over a bit-exact copy.
  function jpegDataFate(prefs) {
    if (prefs.scrub_dct) return 'the JPEG coefficients are scrubbed directly';
    if (prefs.jpeg_canonical) return 'the JPEG data is rebuilt from its coefficients';
    return 'the JPEG data is kept as it is';
  }

  // Why a pixel scrub cannot run while one of the JPEG data switches is on, naming the ones that are.
  function jpegDataBlocks(prefs, lead, goal) {
    const names = jpegDataOptions(prefs);
    const one = names.length === 1;
    return `${lead} ${listed(names)} ${one ? 'is' : 'are'} on: ${one ? 'that option works' : 'those options work'} `
      + `on the compressed JPEG data, not the pixels. Turn ${one ? 'it' : 'them'} off to ${goal}.`;
  }

  function jpegOnly(facts, prefs, what) {
    if (!facts) return ok();
    if (facts.format !== 'JPEG') return na(`${what} applies to JPEG files only. This one is ${nameOf(facts.format)}.`);
    const out = outputFormat(facts, prefs);
    if (out !== 'JPEG') {
      return na(`Not used: this image is being saved as ${nameOf(out)}, which is rebuilt from its pixels.`);
    }
    return ok();
  }

  const smallSide = (facts) => Math.min(facts.size[0], facts.size[1]);

  const RULES = {
    quality(facts, prefs) {
      if (!facts) return ok();
      const out = outputFormat(facts, prefs);
      if (!LOSSY.includes(out)) return na(`${nameOf(out)} is lossless, so it has no quality setting.`);
      if (jpegStays(facts, prefs) && jpegDataOn(prefs)) return na(`Not used: ${jpegDataFate(prefs)}, not re-encoded.`);
      return ok();
    },

    icc(facts) {
      if (!facts) return ok();
      if (!facts.profile) return na('This image has no colour profile.');
      return ok();
    },

    auto_orient(facts) {
      if (!facts) return ok();
      if (!facts.orientation || facts.orientation === 1) return na('This image has no orientation tag.');
      return ok();
    },

    deep(facts, prefs, env) {
      if (!facts) return ok();
      const max = env.limits && env.limits.deep_scan_max_pixels;
      if (max && facts.size[0] * facts.size[1] > max) {
        return no(`The pixel scan is limited to ${thousands(max / 1e6)} megapixels. This image is ${facts.megapixels}.`);
      }
      return ok();
    },

    jpeg_lossless: (facts, prefs) => jpegOnly(facts, prefs, 'Bit-exact mode'),
    scrub_dct: (facts, prefs) => jpegOnly(facts, prefs, 'This scrub'),
    jpeg_canonical: (facts, prefs) => jpegOnly(facts, prefs, 'Canonical rebuilding'),

    scrub_lsb(facts, prefs) {
      if (!facts) return ok();
      if (jpegStays(facts, prefs) && jpegDataOn(prefs)) {
        return no(jpegDataBlocks(prefs, 'It has no effect while', 'scrub pixels'));
      }
      if (facts.frames <= 1 && NO_LSB_MODES.includes(facts.mode)) {
        return no(`${facts.mode === '1' ? '1-bit' : '32-bit'} images have no colour samples to scrub.`);
      }
      return ok();
    },

    scrub_alpha(facts, prefs) {
      if (!facts) return ok();
      if (jpegStays(facts, prefs) && jpegDataOn(prefs)) {
        return no(jpegDataBlocks(prefs, 'It has no effect while', 'clean transparency'));
      }
      if (facts.alpha === false) return na('This image has no transparency channel to clean.');
      return ok();
    },

    scrub_prnu(facts, prefs, env) {
      if (!facts) return ok();
      if (jpegStays(facts, prefs) && jpegDataOn(prefs)) {
        return no(jpegDataBlocks(prefs, 'It changes pixels, so it cannot be used while', 'disrupt the fingerprint'));
      }
      const min = (env.limits && env.limits.prnu_min_side) || 0;
      if (smallSide(facts) < min) {
        return no(`The image is too small to hold a camera fingerprint: ${facts.size[0]}×${facts.size[1]}, under ${min} pixels on a side.`);
      }
      if (facts.frames <= 1 && NO_FINGERPRINT_MODES.includes(facts.mode)) {
        return no('Palette, 1-bit and 32-bit images carry no camera fingerprint and cannot be resampled without changing their colours.');
      }
      return ok();
    },

    optimize(facts, prefs) {
      if (!facts) return ok();
      const out = outputFormat(facts, prefs);
      if (out !== 'PNG') return na(`Only PNG output is recompressed. This image is being saved as ${nameOf(out)}.`);
      if (facts.frames > 1 && ANIMATED_SOURCES.includes(facts.format)) return na('Animated PNGs are not recompressed.');
      return ok();
    },

    random_name: () => ok(),

    c2pa(facts, prefs, env) {
      if (!facts) return ok();
      const out = outputFormat(facts, prefs);
      if (env.signable && !env.signable.includes(out)) {
        return no(`${nameOf(out)} files cannot carry Content Credentials. Save as PNG, JPEG or WebP to sign.`);
      }
      return ok();
    },
  };

  /** {option: {state, reason}} for one image. */
  function evaluate(facts, prefs, env = {}) {
    const out = {};
    for (const name of OPTION_NAMES) out[name] = RULES[name](facts || null, prefs, env);
    return out;
  }

  /** One image's states for each output format: the ones its size or colour depth rule out. */
  function formatStates(facts, env = {}) {
    const out = {};
    for (const fmt of env.formats || []) {
      out[fmt] = ok();
      if (!facts) continue;
      const side = Math.max(facts.size[0], facts.size[1]);
      const max = env.limits && env.limits.max_side && env.limits.max_side[fmt];
      if (max && side > max) {
        out[fmt] = no(`${nameOf(fmt)} cannot hold more than ${thousands(max)} pixels on a side. This image is ${thousands(side)}.`);
      } else if (facts.frames <= 1 && ['I', 'F'].includes(facts.mode) && fmt !== 'TIFF') {
        out[fmt] = no('32-bit images can only be saved as TIFF.');
      }
    }
    return out;
  }

  /** Why an image cannot be cleaned at all (null when it can): size and pixel limits. */
  function blocker(facts, bytes, env = {}) {
    const limits = env.limits || {};
    if (limits.max_upload && bytes > limits.max_upload) {
      return `The file is over the ${humanBytes(limits.max_upload)} limit for one image.`;
    }
    if (facts && limits.max_pixels && facts.size[0] * facts.size[1] > limits.max_pixels) {
      return `The image is ${facts.size[0]}×${facts.size[1]} (${facts.megapixels} megapixels), over the ${thousands(limits.max_pixels / 1e6)} megapixel limit.`;
    }
    return null;
  }

  /** The options to send for one image: prefs, with every option that cannot apply to it switched off. */
  function effective(prefs, states) {
    const out = { ...prefs };
    for (const name of OPTION_NAMES) {
      if (states[name] && states[name].state !== OK) out[name] = OFF[name];
    }
    return out;
  }

  return { OK, NA, UNSUPPORTED, OPTION_NAMES, evaluate, formatStates, blocker, effective, outputFormat };
});
