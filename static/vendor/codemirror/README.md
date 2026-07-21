# CodeMirror 6 browser bundle

`codemirror-6.0.1.bundle.js` is a local ESM bundle built with esbuild from these MIT-licensed packages:

- `@codemirror/state@6.5.2`
- `@codemirror/view@6.38.1`
- `@codemirror/commands@6.8.1`
- `@codemirror/language@6.11.2`
- `@codemirror/autocomplete@6.18.6`
- `@codemirror/lint@6.8.5`
- `@codemirror/search@6.5.11`

The bundle exports only the modules used by `static/dashboard/js/simc-apl-editor.js` and has no runtime CDN dependency.
