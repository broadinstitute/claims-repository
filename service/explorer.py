"""The GraphiQL explorer served at GET /v1/graphql, opened on the example
queries in api/graphql/examples (one tab per example, with its variables).

GraphiQL keeps a visitor's tabs in localStorage and shows them instead of the
default tabs. Its storage is namespaced by a digest of the examples, so a
visitor sees the current examples whenever they change, and their own edits
otherwise.
"""
from __future__ import annotations

import hashlib
import html
import json
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "api" / "graphql" / "examples"

# The GraphiQL build ariadne's explorer template uses (ariadne 0.26), pinned here.
GRAPHIQL = "5.2.2"
GRAPHIQL_REACT = "0.37.3"
GRAPHIQL_TOOLKIT = "0.11.3"
REACT = "19.1.0"
GRAPHQL = "16.11.0"


def examples() -> list[dict]:
    """[{name, query, variables}] in file order."""
    out = []
    for path in sorted(EXAMPLES.glob("*.graphql")):
        variables_path = path.with_suffix(".variables.json")
        variables = json.loads(variables_path.read_text()) if variables_path.exists() else {}
        out.append({"name": path.stem, "query": path.read_text(),
                    "variables": json.dumps(variables, indent=2) if variables else ""})
    return out


def render(title: str = "Claims Repository") -> str:
    tabs = [{"query": e["query"], "variables": e["variables"]} for e in examples()]
    payload = json.dumps(tabs, sort_keys=True)
    namespace = "claims-graphiql-" + hashlib.sha256(payload.encode()).hexdigest()[:12]
    data = payload.replace("<", "\\u003c")  # safe inside <script>
    esm = "https://esm.sh"
    graphiql = f"{esm}/graphiql@{GRAPHIQL}?standalone&external=react,react-dom,@graphiql/react,graphql"
    react_pkg = (f"{esm}/@graphiql/react@{GRAPHIQL_REACT}?standalone&external=react,react-dom,graphql,"
                 "@graphiql/toolkit,@emotion/is-prop-valid")
    toolkit = f"{esm}/@graphiql/toolkit@{GRAPHIQL_TOOLKIT}?standalone&external=graphql"
    importmap = json.dumps({"imports": {
        "react": f"{esm}/react@{REACT}", "react/": f"{esm}/react@{REACT}/",
        "react-dom": f"{esm}/react-dom@{REACT}", "react-dom/": f"{esm}/react-dom@{REACT}/",
        "graphiql": graphiql, "graphiql/": f"{esm}/graphiql@{GRAPHIQL}/",
        "@graphiql/react": react_pkg, "@graphiql/toolkit": toolkit,
        "graphql": f"{esm}/graphql@{GRAPHQL}", "@emotion/is-prop-valid": "data:text/javascript,",
    }}, indent=2)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{html.escape(title)}</title>
    <style>
      body {{ margin: 0; }}
      #graphiql {{ height: 100dvh; }}
      .loading {{ height: 100%; display: flex; align-items: center; justify-content: center; font-size: 2rem; }}
    </style>
    <link rel="stylesheet" href="{esm}/graphiql@{GRAPHIQL}/dist/style.css" crossorigin="anonymous" />
    <script type="importmap">{importmap}</script>
    <script type="application/json" id="example-tabs">{data}</script>
    <script type="module">
      import React from 'react';
      import ReactDOM from 'react-dom/client';
      import {{ GraphiQL }} from 'graphiql';
      import {{ createGraphiQLFetcher }} from '@graphiql/toolkit';
      import 'graphiql/setup-workers/esm.sh';

      const tabs = JSON.parse(document.getElementById('example-tabs').textContent);
      const NAMESPACE = {json.dumps(namespace)};
      const storage = {{
        getItem: (key) => {{ try {{ return localStorage.getItem(NAMESPACE + ':' + key); }} catch {{ return null; }} }},
        setItem: (key, value) => {{ try {{ localStorage.setItem(NAMESPACE + ':' + key, value); }} catch {{}} }},
        removeItem: (key) => {{ try {{ localStorage.removeItem(NAMESPACE + ':' + key); }} catch {{}} }},
        clear: () => {{}},
        key: () => null,
        length: 0,
      }};
      const fetcher = createGraphiQLFetcher({{ url: window.location.origin + window.location.pathname }});
      ReactDOM.createRoot(document.getElementById('graphiql')).render(
        React.createElement(GraphiQL, {{
          fetcher,
          storage,
          defaultTabs: tabs,
          // GraphiQL leaves the first tab's editors empty on a fresh load unless they are seeded too.
          initialQuery: tabs[0] ? tabs[0].query : undefined,
          initialVariables: tabs[0] ? tabs[0].variables : undefined,
          defaultEditorToolsVisibility: 'variables',
        }})
      );
    </script>
  </head>
  <body>
    <div id="graphiql"><div class="loading">Loading {html.escape(title)}…</div></div>
  </body>
</html>
"""
