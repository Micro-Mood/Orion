/**
 * CodeMirror 6 Editor — lazy-loaded wrapper for Orion
 * Loaded via dynamic import() from app.js
 * Exposes window.OrionEditor
 *
 * Uses a LOCAL pre-built single-file bundle (cm6-bundle.js) that contains
 * ALL @codemirror/* and @lezer/* packages in one file — zero version
 * conflicts, zero CDN dependency resolution issues.
 */

let _cm = null;
let _loading = null;

async function ensureCM() {
    if (_cm) return _cm;
    if (_loading) return _loading;
    _loading = import('./cm6-bundle.js').then(m => { _cm = m; return m; });
    return _loading;
}

/* ---------- Language detection ---------- */
const _extToLang = {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    jsx: 'javascript', tsx: 'typescript',
    ts: 'typescript', mts: 'typescript',
    py: 'python', pyw: 'python',
    html: 'html', htm: 'html', vue: 'html', svelte: 'html',
    css: 'css', scss: 'css', less: 'css',
    json: 'json', jsonc: 'json',
    md: 'markdown', mdx: 'markdown',
    sql: 'sql',
    xml: 'xml', svg: 'xml',
    rs: 'rust',
    c: 'cpp', cpp: 'cpp', cc: 'cpp', cxx: 'cpp', h: 'cpp', hpp: 'cpp',
    java: 'java', kt: 'java',
    php: 'php',
};

function getLangExt(cm, fileName) {
    const ext = (fileName.split('.').pop() || '').toLowerCase();
    const key = _extToLang[ext];
    if (!key) return [];
    // All language functions are exported from the bundle
    const langFns = {
        javascript: () => cm.javascript(),
        typescript: () => cm.javascript({ typescript: true }),
        python: () => cm.python(),
        html: () => cm.html(),
        css: () => cm.css(),
        json: () => cm.json(),
        markdown: () => cm.markdown(),
        xml: () => cm.xml(),
        sql: () => cm.sql(),
        rust: () => cm.rust(),
        cpp: () => cm.cpp(),
        java: () => cm.java(),
        php: () => cm.php(),
    };
    const fn = langFns[key];
    if (!fn) return [];
    try { return [fn()]; } catch { return []; }
}

/* ---------- Editor instance ---------- */
let _view = null;
let _langComp = null;
let _darkTheme = null;

function getDarkTheme(cm) {
    if (_darkTheme) return _darkTheme;

    // One Dark Pro Mix theme colors (bg matched to app's --bg-editor)
    const theme = cm.EditorView.theme({
        '&': { backgroundColor: '#1e1e1e', color: '#abb2bf' },
        '.cm-content': { caretColor: '#528bff' },
        '.cm-cursor, .cm-dropCursor': { borderLeftColor: '#528bff' },
        '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
            { backgroundColor: '#3e4451 !important' },
        '.cm-activeLine': { backgroundColor: '#2c313c50' },
        '.cm-gutters': { backgroundColor: '#1e1e1e', color: '#495162', border: 'none' },
        '.cm-activeLineGutter': { backgroundColor: '#2c313c', color: '#abb2bf' },
        '.cm-matchingBracket':
            { backgroundColor: '#515a6b', color: '#abb2bf' },
        '.cm-searchMatch': { backgroundColor: '#d19a6644', outline: '1px solid #d19a6688' },
        '.cm-selectionMatch': { backgroundColor: '#3e4451' },
        '.cm-foldPlaceholder': { backgroundColor: '#3e4451', border: 'none', color: '#abb2bf', padding: '0 4px', borderRadius: '3px' },
        '.cm-tooltip': { border: '1px solid #181a1f', backgroundColor: '#21252b' },
        '.cm-tooltip-autocomplete': {
            '& > ul > li[aria-selected]': { backgroundColor: '#2c313a', color: '#abb2bf' },
        },
    }, { dark: true });

    // One Dark Pro Mix syntax highlighting
    const { tags } = cm;
    const highlight = cm.syntaxHighlighting(cm.HighlightStyle.define([
        { tag: tags.keyword, color: '#c678dd' },
        { tag: tags.operator, color: '#56b6c2' },
        { tag: tags.operatorKeyword, color: '#c678dd' },
        { tag: [tags.variableName], color: '#e06c75' },
        { tag: [tags.function(tags.variableName), tags.function(tags.propertyName)], color: '#61afef' },
        { tag: [tags.definition(tags.variableName)], color: '#e06c75' },
        { tag: [tags.definition(tags.function(tags.variableName))], color: '#61afef' },
        { tag: tags.labelName, color: '#e06c75' },
        { tag: tags.propertyName, color: '#e06c75' },
        { tag: [tags.color, tags.constant(tags.name), tags.standard(tags.name)], color: '#d19a66' },
        { tag: [tags.typeName, tags.className, tags.namespace], color: '#e5c07b' },
        { tag: [tags.changed, tags.annotation, tags.modifier, tags.self], color: '#e5c07b' },
        { tag: [tags.number], color: '#d19a66' },
        { tag: [tags.string, tags.special(tags.string)], color: '#98c379' },
        { tag: [tags.processingInstruction, tags.inserted], color: '#98c379' },
        { tag: [tags.meta, tags.comment], color: '#5c6370' },
        { tag: tags.strong, fontWeight: 'bold', color: '#d19a66' },
        { tag: tags.emphasis, fontStyle: 'italic', color: '#c678dd' },
        { tag: tags.strikethrough, textDecoration: 'line-through' },
        { tag: tags.link, color: '#56b6c2', textDecoration: 'underline' },
        { tag: tags.heading, fontWeight: 'bold', color: '#e06c75' },
        { tag: [tags.atom, tags.bool], color: '#d19a66' },
        { tag: tags.special(tags.variableName), color: '#56b6c2' },
        { tag: [tags.url, tags.escape], color: '#56b6c2' },
        { tag: tags.regexp, color: '#56b6c2' },
        { tag: tags.tagName, color: '#e06c75' },
        { tag: tags.attributeName, color: '#d19a66' },
        { tag: tags.attributeValue, color: '#98c379' },
        { tag: tags.invalid, color: '#ffffff', backgroundColor: '#e06c75' },
        { tag: tags.separator, color: '#abb2bf' },
        { tag: [tags.deleted, tags.character, tags.macroName], color: '#e06c75' },
        { tag: tags.name, color: '#abb2bf' },
    ]));

    _darkTheme = [theme, highlight];
    return _darkTheme;
}

window.OrionEditor = {
    async create(container, content, fileName, { onSave, onChange } = {}) {
        const cm = await ensureCM();

        if (_view) { _view.destroy(); _view = null; }

        _langComp = new cm.Compartment();

        const langExt = getLangExt(cm, fileName);

        const exts = [
            cm.basicSetup,
            ...getDarkTheme(cm),
            _langComp.of(langExt),
            cm.EditorView.updateListener.of(update => {
                if (update.docChanged && onChange) onChange();
            }),
        ];

        if (onSave) {
            exts.push(cm.keymap.of([{
                key: 'Mod-s',
                run: () => { onSave(); return true; },
            }]));
        }

        _view = new cm.EditorView({
            state: cm.EditorState.create({ doc: content, extensions: exts }),
            parent: container,
        });
    },

    destroy() {
        if (_view) { _view.destroy(); _view = null; }
    },

    getValue() {
        return _view ? _view.state.doc.toString() : '';
    },

    setValue(content) {
        if (!_view) return;
        if (_view.state.doc.toString() === content) return;
        _view.dispatch({
            changes: { from: 0, to: _view.state.doc.length, insert: content },
        });
    },
};
