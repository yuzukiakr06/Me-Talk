/* Me Talk 自作スタンプ集。

カラフルな塗りのSVGスタンプ。絵文字は使わず、全てパスで描画する。
stamp(name, size) でSVG文字列を返す。STAMP_LIST が一覧の順序を定義する。

Dev yuzuki_akrdev.ofc
*/

const STAMP_DEFS = {
    hello: {
        label: "やあ",
        svg: '<circle cx="32" cy="32" r="26" fill="#34d399"/><circle cx="24" cy="28" r="3.5" fill="#0b3d2e"/><circle cx="40" cy="28" r="3.5" fill="#0b3d2e"/><path d="M22 40c4 5 16 5 20 0" fill="none" stroke="#0b3d2e" stroke-width="3" stroke-linecap="round"/><path d="M48 18l6-4M50 24l7-1" stroke="#facc15" stroke-width="3" stroke-linecap="round"/>'
    },
    love: {
        label: "だいすき",
        svg: '<path d="M32 54C10 38 14 18 26 18c5 0 6 4 6 4s1-4 6-4c12 0 16 20-6 36z" fill="#f472b6"/><circle cx="24" cy="30" r="2.6" fill="#fff"/><circle cx="40" cy="30" r="2.6" fill="#fff"/>'
    },
    good: {
        label: "いいね",
        svg: '<rect x="14" y="30" width="10" height="20" rx="2" fill="#38bdf8"/><path d="M26 30l7-16c3-1 6 1 5 5l-2 8h12c3 0 5 2 4 5l-4 14c-1 2-3 3-5 3H26z" fill="#22d3ee"/>'
    },
    laugh: {
        label: "わらい",
        svg: '<circle cx="32" cy="32" r="26" fill="#facc15"/><path d="M20 26l8 3-8 3zM44 26l-8 3 8 3" fill="#7c4a03"/><path d="M18 38c6 12 22 12 28 0z" fill="#7c4a03"/><path d="M20 39c5 8 19 8 24 0z" fill="#fff"/>'
    },
    cry: {
        label: "ぴえん",
        svg: '<circle cx="32" cy="32" r="26" fill="#a5b4fc"/><path d="M22 28c2-2 6-2 8 0M34 28c2-2 6-2 8 0" fill="none" stroke="#1e293b" stroke-width="2.5" stroke-linecap="round"/><ellipse cx="32" cy="44" rx="5" ry="7" fill="#1e293b"/><path d="M22 34c-1 6 0 10 0 10M42 34c1 6 0 10 0 10" stroke="#38bdf8" stroke-width="3" stroke-linecap="round" fill="none"/>'
    },
    congrats: {
        label: "おめでとう",
        svg: '<path d="M14 52l10-30 18 18z" fill="#fb7185"/><path d="M14 52l10-30 8 8z" fill="#f43f5e"/><circle cx="44" cy="16" r="4" fill="#facc15"/><circle cx="52" cy="26" r="3" fill="#34d399"/><circle cx="38" cy="12" r="3" fill="#38bdf8"/><path d="M44 16l6-6M52 26l6-3M38 12l3-6" stroke="#facc15" stroke-width="2" stroke-linecap="round"/>'
    },
    thanks: {
        label: "ありがとう",
        svg: '<circle cx="32" cy="32" r="26" fill="#fdba74"/><path d="M22 28h4M38 28h4" stroke="#7c2d12" stroke-width="3" stroke-linecap="round"/><path d="M24 40c4 4 12 4 16 0" fill="none" stroke="#7c2d12" stroke-width="3" stroke-linecap="round"/><path d="M32 44l-3 8h6z" fill="#f87171"/>'
    },
    sleep: {
        label: "おやすみ",
        svg: '<path d="M40 12a22 22 0 1 0 12 34A18 18 0 0 1 40 12z" fill="#c4b5fd"/><path d="M40 22h10l-10 10h10" stroke="#6d28d9" stroke-width="3" fill="none" stroke-linecap="round" stroke-linejoin="round"/><circle cx="26" cy="34" r="2.5" fill="#4c1d95"/><circle cx="36" cy="38" r="2" fill="#4c1d95"/>'
    }
};

const STAMP_LIST = ["hello", "love", "good", "laugh", "cry", "congrats", "thanks", "sleep"];

const NEMU_STAMPS = [
    { key: "ohayou", label: "おはよう" },
    { key: "konnichiwa", label: "こんにちは" },
    { key: "konbanwa", label: "こんばんは" },
    { key: "oyasumi", label: "おやすみ" },
    { key: "yaa", label: "やあ" },
    { key: "daisuki", label: "だいすき" },
    { key: "iine", label: "いいね" },
    { key: "uow", label: "うおw" },
    { key: "pien", label: "ぴえん" },
    { key: "omedetou", label: "おめでとう" },
    { key: "arigatou", label: "ありがとう" },
];

const STAMP_CATEGORIES = [
    { id: "nemu", label: "Nemu", type: "image", items: NEMU_STAMPS },
    { id: "basic", label: "ベーシック", type: "svg", items: STAMP_LIST.map((k) => ({ key: k, label: STAMP_DEFS[k].label })) },
];

function nemuStampUrl(key) {
    return `/static/stamps/nemu_${key}.png`;
}

function stamp(name, size) {
    const def = STAMP_DEFS[name];
    if (!def) return "";
    const s = size || 96;
    return `<svg viewBox="0 0 64 64" width="${s}" height="${s}" aria-hidden="true">${def.svg}</svg>`;
}
