// Shared 24 px geometry for the product and the downloadable design assets.
export const iconPaths = Object.freeze({
  call: '<path d="M8 3H4.5A1.5 1.5 0 0 0 3 4.5C3 13.6 10.4 21 19.5 21a1.5 1.5 0 0 0 1.5-1.5V16l-5-2-2 2a14 14 0 0 1-6-6l2-2-2-5Z"/>',
  directions: '<path d="m12 2 10 10-10 10L2 12 10-10Z"/><path d="M8 15v-4h8m-3-3 3 3-3 3"/>',
  save: '<path d="M6 3h12v18l-6-4-6 4V3Z"/>',
  source: '<path d="M6 2h8l4 4v16H6V2Z"/><path d="M14 2v5h4M9 11h6m-6 4h6m-6 4h4"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  pin: '<path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z"/><circle cx="12" cy="10" r="2.5"/>',
  locate: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2"/><path d="M12 2v3m0 14v3M2 12h3m14 0h3"/>',
  hospital: '<path d="M5 21V3h14v18M3 21h18M10 21v-5h4v5M9 8h6m-3-3v6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
  bed: '<path d="M3 7v14m18-8v8M3 17h18M3 13h18M11 13V8h7a3 3 0 0 1 3 3v2"/><rect x="5" y="9" width="4" height="4" rx="1"/>',
  team: '<circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3M16 4a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v3"/>',
  check: '<circle cx="12" cy="12" r="9"/><path d="m7.5 12 3 3 6-6"/>',
  alert: '<path d="m10.3 4-8 14a2 2 0 0 0 1.7 3h16a2 2 0 0 0 1.7-3l-8-14a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4m0 4h.01"/>',
  close: '<path d="m6 6 12 12M18 6 6 18"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  reset: '<path d="M3 10a9 9 0 1 1 2 8M3 4v6h6"/>',
  chevron: '<path d="m5 9 7 7 7-7"/>',
  settings: '<path d="M4 7h9m4 0h3M4 17h3m4 0h9"/><circle cx="15" cy="7" r="2"/><circle cx="9" cy="17" r="2"/>',
  plus: '<path d="M12 4v16M4 12h16"/>',
  star: '<path d="m12 3 2.8 5.7 6.3.9-4.6 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L2.9 9.6l6.3-.9L12 3Z"/>',
});
export const iconMarkup = name => `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${iconPaths[name] || ''}</svg>`;
