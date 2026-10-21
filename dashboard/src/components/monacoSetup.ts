/**
 * Monaco editor setup — bundles monaco locally instead of fetching it from a
 * public CDN at runtime.
 *
 * Why: `@monaco-editor/react` defaults to injecting
 * `https://cdn.jsdelivr.net/npm/monaco-editor@<ver>/min/vs/loader.js` when
 * `loader.config` is never called. cdn.jsdelivr.net is intermittently
 * unreachable from some networks (connection reset), which left the
 * expert-md edit drawer stuck on "Loading...". Serving monaco from the app
 * bundle removes the runtime CDN dependency entirely.
 *
 * Usage: import ONLY via `lazy(() => import("@/components/monacoSetup"))` so
 * the heavy editor bundle stays out of the initial page load. This module
 * configures the loader with the bundled monaco instance, then re-exports
 * the editor component as its default export.
 *
 * T4 pluggability: the only upstream footprint is the single lazy-import
 * line in FileEditModal.tsx and CodeEditor.tsx. To unplug, delete this file
 * and point those imports back at "@monaco-editor/react" — the pre-feature
 * CDN behaviour is restored verbatim.
 */
import { loader } from "@monaco-editor/react";
import * as monaco from "monaco-editor/esm/vs/editor/editor.api.js";

// Core editor features and their CSS (find, folding, hover, suggest, ...).
import "monaco-editor/esm/vs/editor/editor.all.js";
// Monarch tokenizers for the languages mapped in
// pages/Agent/Workspace/utils/editorLanguage.ts. The `c` tokenizer lives in
// the cpp contribution. Rich language services are intentionally limited to
// JSON to keep the bundle small — other languages get tokenization only.
import "monaco-editor/esm/vs/basic-languages/typescript/typescript.contribution.js";
import "monaco-editor/esm/vs/basic-languages/javascript/javascript.contribution.js";
import "monaco-editor/esm/vs/basic-languages/python/python.contribution.js";
import "monaco-editor/esm/vs/basic-languages/markdown/markdown.contribution.js";
import "monaco-editor/esm/vs/basic-languages/css/css.contribution.js";
import "monaco-editor/esm/vs/basic-languages/less/less.contribution.js";
import "monaco-editor/esm/vs/basic-languages/scss/scss.contribution.js";
import "monaco-editor/esm/vs/basic-languages/html/html.contribution.js";
import "monaco-editor/esm/vs/basic-languages/xml/xml.contribution.js";
import "monaco-editor/esm/vs/basic-languages/yaml/yaml.contribution.js";
import "monaco-editor/esm/vs/basic-languages/ini/ini.contribution.js";
import "monaco-editor/esm/vs/basic-languages/shell/shell.contribution.js";
import "monaco-editor/esm/vs/basic-languages/sql/sql.contribution.js";
import "monaco-editor/esm/vs/basic-languages/go/go.contribution.js";
import "monaco-editor/esm/vs/basic-languages/rust/rust.contribution.js";
import "monaco-editor/esm/vs/basic-languages/java/java.contribution.js";
import "monaco-editor/esm/vs/basic-languages/kotlin/kotlin.contribution.js";
import "monaco-editor/esm/vs/basic-languages/ruby/ruby.contribution.js";
import "monaco-editor/esm/vs/basic-languages/php/php.contribution.js";
import "monaco-editor/esm/vs/basic-languages/cpp/cpp.contribution.js";
import "monaco-editor/esm/vs/basic-languages/csharp/csharp.contribution.js";
import "monaco-editor/esm/vs/basic-languages/swift/swift.contribution.js";
import "monaco-editor/esm/vs/basic-languages/dockerfile/dockerfile.contribution.js";
// JSON language service (schema validation, formatting) + its worker.
import "monaco-editor/esm/vs/language/json/monaco.contribution.js";
import JsonWorker from "monaco-editor/esm/vs/language/json/json.worker.js?worker";
import EditorWorker from "monaco-editor/esm/vs/editor/editor.worker.js?worker";

self.MonacoEnvironment = {
  getWorker(_workerId: string, label: string): Worker {
    if (label === "json") return new JsonWorker();
    return new EditorWorker();
  },
};

// Serve monaco from the local bundle — no runtime CDN fetch.
loader.config({
  monaco: monaco as unknown as typeof import("monaco-editor"),
});

export { default } from "@monaco-editor/react";
