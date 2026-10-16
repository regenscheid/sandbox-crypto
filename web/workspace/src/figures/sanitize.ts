// SPDX-License-Identifier: MIT
/**
 * The figure frame's sanitizer (docs/specs/figures.md, FG-14; review of #492, L1), for the
 * SVG the chart and diagram libraries draw (F5, F6). An allowlist: the markup is parsed as
 * XML into a document of its own, which loads and runs nothing, and only SVG elements and
 * attributes this file names survive, with no value that could reach an address; then the
 * tree is adopted into the page. Animation, styles, links, images, `use`, scripts and
 * foreign content are not on the list, nor any attribute that names an address. A
 * reference within the figure, `url(#id)`, is kept: gradients, clips and markers use it.
 *
 * Not DOMPurify: its output depends on the browser's HTML parser, which the tests' DOM does
 * not reproduce for SVG (it kept `<set>` and dropped `<svg>`), so a list checked by the
 * tests is the one that runs. It lives apart from the protocol, which the workspace's
 * client and the PDF viewer bundle.
 */

const SVG = 'http://www.w3.org/2000/svg';

const TAGS = new Set(
  (
    'svg g defs title desc path rect circle ellipse line polyline polygon text tspan ' +
    'clipPath mask marker linearGradient radialGradient stop pattern filter feBlend ' +
    'feColorMatrix feComponentTransfer feComposite feConvolveMatrix feDiffuseLighting ' +
    'feDisplacementMap feDistantLight feDropShadow feFlood feFuncA feFuncB feFuncG feFuncR ' +
    'feGaussianBlur feMerge feMergeNode feMorphology feOffset fePointLight ' +
    'feSpecularLighting feSpotLight feTile feTurbulence'
  ).split(' '),
);

const ATTRIBUTES = new Set(
  (
    'id class style x y x1 y1 x2 y2 cx cy r rx ry fx fy fr width height d points transform ' +
    'viewBox preserveAspectRatio fill fill-opacity fill-rule stroke stroke-width ' +
    'stroke-opacity stroke-dasharray stroke-dashoffset stroke-linecap stroke-linejoin ' +
    'stroke-miterlimit opacity color font-family font-size font-weight font-style ' +
    'font-variant text-anchor text-decoration dominant-baseline alignment-baseline ' +
    'baseline-shift letter-spacing word-spacing writing-mode dx dy rotate textLength ' +
    'lengthAdjust clip-path clip-rule clipPathUnits mask maskUnits maskContentUnits ' +
    'marker-start marker-mid marker-end markerWidth markerHeight markerUnits refX refY ' +
    'orient gradientUnits gradientTransform spreadMethod offset stop-color stop-opacity ' +
    'patternUnits patternContentUnits patternTransform filter filterUnits primitiveUnits ' +
    'in in2 result stdDeviation mode operator k1 k2 k3 k4 values type tableValues slope ' +
    'intercept amplitude exponent order kernelMatrix divisor bias targetX targetY ' +
    'edgeMode kernelUnitLength preserveAlpha surfaceScale diffuseConstant ' +
    'specularConstant specularExponent lighting-color azimuth elevation z ' +
    'pointsAtX pointsAtY pointsAtZ limitingConeAngle scale xChannelSelector ' +
    'yChannelSelector flood-color flood-opacity radius baseFrequency numOctaves seed ' +
    'stitchTiles visibility display overflow vector-effect shape-rendering ' +
    'text-rendering role aria-label aria-hidden aria-roledescription'
  ).split(' '),
);

/** A value that could load, import or run something; a reference within the figure,
 * `url(#id)`, is not one. */
function reaches(value: string): boolean {
  const local = value.replace(/url\(\s*(['"]?)#[\w.:-]+\1\s*\)/gi, '');
  return /url\s*\(|@import|image-set\s*\(|expression\s*\(|javascript:|\\/i.test(local);
}

function clean(element: Element): void {
  for (const attribute of [...element.attributes]) {
    const allowed =
      attribute.namespaceURI === null &&
      ATTRIBUTES.has(attribute.localName) &&
      !reaches(attribute.value);
    if (!allowed) element.removeAttributeNode(attribute);
  }
  for (const child of [...element.childNodes]) {
    if (child.nodeType === child.TEXT_NODE) continue;
    if (
      child.nodeType === child.ELEMENT_NODE &&
      (child as Element).namespaceURI === SVG &&
      TAGS.has((child as Element).localName)
    )
      clean(child as Element);
    else child.remove(); // other elements, comments, processing instructions, CDATA
  }
}

/** What a library drew, as SVG markup, made inert and adopted into `into`; an empty
 * fragment for markup that is not one SVG document. */
export function sanitized(markup: string, into: Document): DocumentFragment {
  const fragment = into.createDocumentFragment();
  const parsed = new DOMParser().parseFromString(markup, 'image/svg+xml');
  const root = parsed.documentElement;
  if (
    root === null ||
    root.namespaceURI !== SVG ||
    root.localName !== 'svg' ||
    parsed.getElementsByTagName('parsererror').length > 0
  )
    return fragment;
  clean(root);
  fragment.append(into.adoptNode(root));
  return fragment;
}
