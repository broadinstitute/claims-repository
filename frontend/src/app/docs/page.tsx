import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Retrieving results",
  description: "How to retrieve and cite results from the claims repository, and why it is built the way it is, with PIGEAN as the worked example.",
  robots: { index: true, follow: true },
};

/** The API these docs point at. CLAIMS_DOCS_API_URL overrides it per deployment. */
const API = (process.env.CLAIMS_DOCS_API_URL || "https://api-qa.hugeampkpnbi.org/api/claims").replace(/\/+$/, "");

const SECTIONS = [
  ["principles", "Design principles"],
  ["model", "The pieces you will meet"],
  ["discover", "1. See what you can ask"],
  ["values", "2. Find the values a parameter takes"],
  ["run", "3. Run a query"],
  ["rows", "4. Read the rows"],
  ["claims", "5. Cite and check a claim"],
  ["own", "6. Ask your own question"],
  ["objects", "7. Look up any object"],
  ["example", "Worked example: T2D with and without BMI"],
  ["graphql", "GraphQL"],
  ["agents", "Notes for agents and pipelines"],
] as const;

function Code({ children }: { children: ReactNode }) {
  return <code className="rounded bg-muted/60 px-1 py-0.5 font-mono text-[0.85em]">{children}</code>;
}

function Block({ children, label }: { children: string; label?: string }) {
  return (
    <figure className="my-3 min-w-0">
      {label && <figcaption className="mb-1 text-xs uppercase tracking-wide text-muted-foreground">{label}</figcaption>}
      <pre className="overflow-x-auto rounded-md border border-border/60 bg-muted/30 p-3 font-mono text-[12.5px] leading-5">{children}</pre>
    </figure>
  );
}

function Section({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-6 space-y-3 border-t border-border/60 pt-8">
      <h2 className="text-xl font-semibold tracking-tight [text-wrap:balance]">{title}</h2>
      {children}
    </section>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="my-3 overflow-x-auto">
      <table className="w-full border-collapse text-sm">
        <thead>
          <tr>{head.map(h => <th key={h} className="border-b border-border py-2 pr-4 text-left text-xs font-medium uppercase tracking-wide text-muted-foreground">{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j} className="border-b border-border/60 py-2 pr-4 align-top">{c}</td>)}</tr>)}
        </tbody>
      </table>
    </div>
  );
}

export default function DocsPage() {
  return (
    <div className="min-h-dvh bg-background text-foreground">
      <div className="mx-auto grid max-w-6xl gap-10 px-4 py-10 sm:px-6 lg:grid-cols-[14rem_minmax(0,1fr)]">
        <nav aria-label="On this page" className="lg:sticky lg:top-6 lg:self-start">
          <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">On this page</p>
          <ul className="space-y-1.5 text-sm">
            {SECTIONS.map(([id, title]) => <li key={id}><a href={`#${id}`} className="text-muted-foreground hover:text-foreground">{title}</a></li>)}
          </ul>
          <p className="mt-6 text-xs text-muted-foreground">API: <span className="break-all font-mono">{API}</span></p>
        </nav>

        <main className="min-w-0 max-w-3xl space-y-8 leading-7 [&_p]:max-w-[68ch]">
          <header className="space-y-3">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Claims repository</p>
            <h1 className="text-3xl font-semibold tracking-tight [text-wrap:balance]">Retrieving results</h1>
            <p className="text-muted-foreground">
              The claims repository serves analysis results as DAPPER claims: every number comes with an id, the statement it
              makes, its scores, and evidence that points at the exact bytes in the result file it came from. This page explains
              how to retrieve them and why the service is built the way it is. PIGEAN gene and gene-set associations for type 2
              diabetes are the worked example; every step works the same way for any collection.
            </p>
            <div className="rounded-lg border border-border/60 bg-muted/30 px-4 py-3 text-sm">
              Every example below runs as written against <span className="font-mono">{API}</span>.
            </div>
          </header>

          <Section id="principles" title="Design principles">
            <dl className="space-y-4">
              {[
                ["Every number is a claim.",
                  "A result is never a bare value. It is served with the claim that states it: what is asserted about which entities, the scores, the method that produced them, and evidence locating each number in its source file by byte range and sha256. A value you keep can be cited by id and re-checked against its file by anyone."],
                ["Identity is content.",
                  "Claims and every other object are addressed by a digest of their content (DAPPER-ID-1). The same statement from the same data has the same id wherever it is served, so ids can be compared, cached and cited across systems without coordination."],
                ["One way to ask, for every method.",
                  "There is no endpoint per method. Each collection of results publishes named query templates, and one call runs any of them. A new method arrives as a new collection, and clients need no new code."],
                ["The service describes itself.",
                  "A catalog lists every query with its parameters, the values they take, the rows it returns and what each score means. It is generated from the release the queries read, so it cannot drift from what runs. A person or an agent can start from the catalog alone."],
                ["Questions are bounded and stated.",
                  "Templates take variables, not query text. Each one filters, orders and limits before it joins, breaks ties the same way every time, and reports a missing data source as a warning rather than returning an empty answer. The same request returns the same rows."],
                ["Scores keep their meaning.",
                  "Each score carries its scale, direction and interpretation, and the claim that states it carries the same. A query orders and filters only on its own kind of score, so scores from different methods are never blended or ranked together."],
                ["Each collection owns its vocabulary.",
                  "How a collection's keys map to standard ids (a PIGEAN trait key to a KPN trait id, with its ontology cross-references) ships with the collection's release. Nothing depends on a registry that might change underneath it."],
              ].map(([term, body]) => (
                <div key={term}>
                  <dt className="font-medium">{term}</dt>
                  <dd className="text-muted-foreground">{body}</dd>
                </div>
              ))}
            </dl>
          </Section>

          <Section id="model" title="The pieces you will meet">
            <Table head={["Thing", "What it is", "PIGEAN example"]} rows={[
              ["Collection", "One method's results, registered as data, with its own vocabulary and query templates", <Code key="c">pigean</Code>],
              ["Release", "One version of a collection; queries read the current release unless you name another", <Code key="r">2026.11.1</Code>],
              ["Dimension", "What identifies each dataset in a release", <span key="d"><Code>trait</Code>, <Code>library</Code></span>],
              ["Query template", "A named, bounded question with parameters, an ordered plan and a runnable example", <Code key="q">top-genes-with-sets</Code>],
              ["Row", "One result: a subject, an object, its scores and the claims it stands for", "TCF7L2 associated with T2D"],
              ["Claim", "A DAPPER Claim: statement, scores and evidence, addressed by its content", <Code key="cl">dapper:Claim.…</Code>],
            ]} />
          </Section>

          <Section id="discover" title="1. See what you can ask">
            <p>The catalog lists every query of every collection, and a guide to calling them:</p>
            <Block>{`curl ${API}/v1/queries`}</Block>
            <p>Each entry gives:</p>
            <ul className="list-disc space-y-1 pl-5">
              <li><Code>collection</Code>, <Code>name</Code> and <Code>description</Code>;</li>
              <li><Code>params</Code>: each parameter&apos;s type, default and bounds. A parameter that picks a dimension also shows how many values it takes, a sample, and where to list them all;</li>
              <li><Code>returns</Code>: the kind of row, the entities it names, every score with its scale, direction and meaning, and any joined rows;</li>
              <li><Code>claims</Code>: the claim keys rows can serve;</li>
              <li><Code>run</Code>: a REST and a GraphQL request that run as written;</li>
              <li><Code>notes</Code>, when the template&apos;s example names a value this release does not have.</li>
            </ul>
            <p>Add <Code>?collection=pigean</Code> for one collection. PIGEAN has three templates:</p>
            <Table head={["Template", "Answers"]} rows={[
              [<Code key="t">top-genes-with-sets</Code>, "The strongest genes for one trait, each with its strongest gene sets"],
              [<Code key="g">gene-across-traits</Code>, "One gene's support in every trait, strongest first"],
              [<Code key="s">gene-set-across-traits</Code>, "One gene set's effect in every trait, largest first"],
            ]} />
            <p>The catalog also lists, under <Code>collections</Code>, each collection&apos;s row kinds, scores, dimensions and edges: what you need to write your own plan (step 6).</p>
          </Section>

          <Section id="values" title="2. Find the values a parameter takes">
            <p>A dimension parameter takes the values the collection&apos;s datasets have. List or search them:</p>
            <Block>{`curl "${API}/v1/collections/pigean/dimensions/trait?search=T2D"`}</Block>
            <Block label="Two of the matches">{`{"value": "KPN.TRAIT:0000398", "label": "Type 2 diabetes (T2D)", "also_accepts": ["T2D"], "datasets": 1}
{"value": "KPN.TRAIT:0000406", "label": "Type 2 diabetes (T2D) adj BMI", "also_accepts": ["T2DadjBMI"], "datasets": 1}`}</Block>
            <p>
              Each value comes with its label, how many datasets have it, and <Code>also_accepts</Code>: other keys a query
              takes for it. PIGEAN&apos;s traits are KPN trait ids, and its result files&apos; own keys work too
              (<Code>T2D</Code> for <Code>KPN.TRAIT:0000398</Code>). Search matches the value, label or any accepted key.
              Leave out <Code>search</Code> to list them all; <Code>first</Code> limits the list.
            </p>
            <p>The vocabulary belongs to each collection, so the same call works for any collection&apos;s dimensions.</p>
          </Section>

          <Section id="run" title="3. Run a query">
            <Block label="REST">{`curl -X POST ${API}/v1/collections/pigean/queries/top-genes-with-sets:run \\
  -H 'Content-Type: application/json' \\
  -d '{"variables": {"trait": "T2D", "topGenes": 20, "setsPerGene": 3}}'`}</Block>
            <Table head={["Body field", "Meaning"]} rows={[
              [<Code key="v">variables</Code>, "The template's parameters. Unknown names, wrong types and out-of-range values are refused with a message listing the parameters; omitted ones take their default."],
              [<Code key="c">claims</Code>, <>Which claims each row serves. By default every row comes with all the claims that state its numbers. Name the ones you need, such as <Code>[&quot;measurement&quot;]</Code>, or send <Code>[]</Code> for rows alone.</>],
              [<Code key="r">release</Code>, "Optional: a release version or id. The current release by default."],
            ]} />
            <p>For PIGEAN&apos;s template:</p>
            <Table head={["Variable", "Meaning", "Default"]} rows={[
              [<Code key="t">trait</Code>, "KPN trait id or accepted key (required)", ""],
              [<Code key="l">library</Code>, "Gene-set library", <Code key="ld">mouse_msigdb</Code>],
              [<Code key="p">priorMax</Code>, "Keep genes whose gene-set prior is below this, so their support is mostly direct GWAS. Omit for no filter.", "none"],
              [<Code key="g">topGenes</Code>, "Genes returned, strongest combined score first (0 to 200)", "20"],
              [<Code key="s">setsPerGene</Code>, "Gene sets per gene, strongest marginal effect first (0 to 50); 0 for genes only", "10"],
            ]} />
            <p>
              A query is bounded: it filters, orders and limits before any join, breaks ties the same way every time, and says
              in <Code>warnings</Code> when a join has no data to read, so an empty result is never silent.
            </p>
          </Section>

          <Section id="rows" title="4. Read the rows">
            <Block>{`{
  "collection": "pigean", "release": {"id": "dapper:ResultCollection.…", "version": "2026.11.1"},
  "rows": [{
    "kind": "gene_phenotype",
    "dataset": {"id": "dapper:Dataset.…", "dimensions": {"trait": "KPN.TRAIT:0000398", "library": "mouse_msigdb"}},
    "subject": {"id": "HGNC.SYMBOL:TCF7L2", "value": "TCF7L2", "label": "TCF7L2"},
    "object":  {"id": "KPN.TRAIT:0000398", "label": "Type 2 diabetes (T2D)"},
    "metrics": {"combined": 10.9, "log_bf": 5.2, "prior": 5.73},
    "claims":  {"measurement": { … claim envelope … }},
    "sets": [{"edge": {"claims": {"roster": { … }}}, "row": { … a gene_set_phenotype row … }}]
  }],
  "warnings": []
}`}</Block>
            <p>
              <Code>subject</Code> and <Code>object</Code> carry canonical ids (<Code>id</Code>) and the value as the result file
              wrote it (<Code>value</Code>). Scores are in <Code>metrics</Code>; what each means is in the catalog entry&apos;s
              <Code>returns</Code>. For PIGEAN:
            </p>
            <Table head={["Score", "Scale", "Meaning"]} rows={[
              [<Code key="c">combined</Code>, "log posterior odds", "Gene–trait support: direct GWAS (log_bf) plus gene-set evidence (prior). Not a probability. Rank genes by this."],
              [<Code key="b">log_bf</Code>, "log Bayes factor", "Direct GWAS support, from 0 to 5.2. 0 means no direct GWAS signal (most genes); the strongest genes are reported at 5.2 and tie there, so order them by combined or prior."],
              [<Code key="p">prior</Code>, "log prior odds", "Support from the gene sets the gene belongs to. Negative when its sets lower its odds."],
              [<Code key="be">beta</Code>, "log-odds effect", "Gene-set effect estimated jointly with the other selected sets. Near 0 when other sets explain the same signal."],
              [<Code key="bu">beta_uncorrected</Code>, "log-odds effect", "Marginal gene-set effect. A large marginal effect with a joint beta near 0 means the set shares its signal with others."],
            ]} />
            <p>Scores from different collections have different meanings. Compare them within a collection, never across.</p>
          </Section>

          <Section id="claims" title="5. Cite and check a claim">
            <p>
              Each row&apos;s <Code>claims</Code> maps a claim key to an envelope: the claim&apos;s <Code>id</Code>, a
              <Code>resolverUrl</Code>, the DAPPER <Code>claim</Code> and <Code>proposition</Code> documents, the
              <Code>triple</Code> (subject, predicate, object), its <Code>scores</Code> and its <Code>evidence</Code>. PIGEAN rows serve:
            </p>
            <ul className="list-disc space-y-1 pl-5">
              <li><Code>measurement</Code>: the scores as reported, each with evidence;</li>
              <li><Code>tier-*</Code>: an evidence tier for one score under the collection&apos;s declared rule;</li>
              <li><Code>roster</Code> (on joined gene sets): the gene set&apos;s exact membership.</li>
            </ul>
            <p>
              A claim id is a content address: the same claim has the same id wherever it is served, so store the id with any
              value you take. A measurement&apos;s evidence carries value locators: the result file, its sha256, and the byte
              range of the number in it, so anyone can re-check the value against the file.
            </p>
            <p>
              Claims come with every read by default, because every number is a claim. Serving one records that the repository
              stood behind it again (its <Code>timesDeposited</Code>); the claim itself never changes.
            </p>
          </Section>

          <Section id="own" title="6. Ask your own question">
            <p>
              Templates cover the questions a collection&apos;s owners expected. For anything else, write ordinary GraphQL
              against the same rows: <Code>rows</Code> picks a row kind and filters it by dimensions, subject and score
              conditions, orders it and limits it; <Code>joined</Code> follows edges from each row; <Code>claim</Code>
              serves the claim behind a row. The schema documents every field, so GraphiQL autocompletes the whole query.
            </p>
            <Block label="T2D adj BMI genes with no direct GWAS signal, ranked by gene-set support">{`{ rows(collection: "pigean", kind: "gene_phenotype",
       dimensions: [{name: "trait", value: "T2DadjBMI"}],
       where: [{metric: "log_bf", eq: 0}],
       orderBy: {metric: "prior", dir: DESC}, first: 10) {
    rows {
      subject { value }
      prior: metric(name: "prior")
      claim(key: "measurement") { id }
      joined(via: "member_of", kind: "gene_set_phenotype", orderBy: {metric: "beta", dir: DESC}, first: 3) {
        row { subject { value } beta: metric(name: "beta") }
      }
    }
  }
}`}</Block>
            <p>
              The catalog lists each collection&apos;s row kinds, scores, dimensions and edges under <Code>collections</Code>.
              The same rules as a template apply: a condition or order uses a score of the kind it applies to (a gene row on
              <Code>combined</Code>, <Code>log_bf</Code>, <Code>prior</Code>; a gene-set row on <Code>beta</Code>,
              <Code>beta_uncorrected</Code>), so scores of different kinds are never compared, and lists are bounded (500
              rows, 50 joined rows per row). A query that breaks a rule is refused with a message saying why.
            </p>
          </Section>

          <Section id="objects" title="7. Look up any object">
            <Table head={["Call", "Answers"]} rows={[
              [<Code key="o">GET /v1/objects/{"{id}"}</Code>, "Any stored DAPPER object by id: a claim, proposition, file, activity, gene set. In a browser it shows a page with linked references."],
              [<Code key="s">GET /v1/objects/search?references={"{id}"}</Code>, <>Objects that point at an object, e.g. every claim about a proposition (<Code>class_name=Claim</Code>). Also <Code>q</Code> for text and <Code>match=slot:value</Code>.</>],
              [<Code key="d">GET /v1/deposits?object={"{id}"}</Code>, "Which deposits carried an object, and who sent them."],
              [<Code key="c">GET /v1/collections/{"{c}"}</Code>, <>What a collection holds: its dimensions, scores and their meaning, templates and releases, and its datasets a page at a time (<Code>first</Code>, <Code>offset</Code>).</>],
            ]} />
          </Section>

          <Section id="example" title="Worked example: T2D with and without BMI">
            <p>
              Type 2 diabetes risk works partly through body weight and partly through the pancreatic beta cell. PIGEAN has
              results for T2D (<Code>KPN.TRAIT:0000398</Code>) and for T2D adjusted for BMI (<Code>KPN.TRAIT:0000406</Code>).
              Comparing them separates the two. An agent found this using only the GraphQL endpoint and the catalog.
            </p>
            <Block label="Run the same template for both traits">{`curl -X POST ${API}/v1/collections/pigean/queries/top-genes-with-sets:run \
  -H 'Content-Type: application/json' -d '{"variables": {"trait": "T2D", "topGenes": 200, "setsPerGene": 0}}'
curl -X POST ${API}/v1/collections/pigean/queries/top-genes-with-sets:run \
  -H 'Content-Type: application/json' -d '{"variables": {"trait": "T2DadjBMI", "topGenes": 200, "setsPerGene": 0}}'`}</Block>
            <Table head={["Gene", "T2D rank", "T2D log_bf", "T2D adj BMI rank", "T2D adj BMI log_bf"]} rows={[
              ["FTO", "7", "5.2", "27", "3.8"],
              ["MC4R", "24", "3.55", "not in top 200", "—"],
              ["TMEM18", "115", "2.11", "not in top 200", "—"],
              ["KCNJ11", "65", "2.56", "3", "5.15"],
              ["HNF1B", "21", "5.0", "11", "5.2"],
              ["TCF7L2", "1", "5.2", "2", "5.2"],
            ]} />
            <p>
              Adjusting for BMI takes direct GWAS support away from the obesity genes (FTO, MC4R, TMEM18) and leaves the
              beta-cell and monogenic-diabetes genes on top: PPARG, TCF7L2, KCNJ11, HNF4A, SLC30A8, WFS1, CDKAL1. Each value
              in the table is a row with its measurement claim, so every number in a write-up can cite its claim id and point at
              the bytes of the result file it came from. Which genes are &quot;obesity&quot; or &quot;beta-cell&quot; genes is
              outside knowledge; the repository supplies the scores and their meaning.
            </p>
            <Block label="Then follow one gene across every trait">{`curl -X POST ${API}/v1/collections/pigean/queries/gene-across-traits:run \
  -H 'Content-Type: application/json' -d '{"variables": {"gene": "FTO", "topTraits": 20, "setsPerTrait": 0}}'`}</Block>
          </Section>

          <Section id="graphql" title="GraphQL">
            <p>The same calls are GraphQL fields at <Code>POST {API}/v1/graphql</Code>:</p>
            <Table head={["Field", "Same as"]} rows={[
              [<Code key="q">queries</Code>, <Code key="q2">GET /v1/queries</Code>],
              [<Code key="d">dimensionValues(collection, dimension, search)</Code>, <Code key="d2">GET /v1/collections/{"{c}"}/dimensions/{"{d}"}</Code>],
              [<Code key="r">runQuery(collection, query, variables, claims)</Code>, <Code key="r2">POST …/queries/{"{name}"}:run</Code>],
              [<Code key="p">rows(collection, kind, dimensions, subject, where, orderBy, first)</Code>, "Your own question over a collection's rows (step 6)"],
              [<Code key="cl">collection(id, datasetsFirst, datasetsOffset)</Code>, <Code key="cl2">GET /v1/collections/{"{c}"}</Code>],
              [<Code key="c">claim(id)</Code>, "A claim envelope by id"],
              [<Code key="o">dapperObject(id)</Code>, <Code key="o2">GET /v1/objects/{"{id}"}</Code>],
            ]} />
            <Block>{`{"query": "query($v: JSON) { runQuery(collection: \\"pigean\\", query: \\"top-genes-with-sets\\", variables: $v) }",
 "variables": {"v": {"trait": "T2DadjBMI", "topGenes": 20, "setsPerGene": 3}}}`}</Block>
            <p>
              Open <a className="underline underline-offset-4" href={`${API}/v1/graphql`}>{`${API}/v1/graphql`}</a> in a browser for an
              explorer with worked examples. <Code>runQuery</Code> returns JSON: the row shape follows each collection, not a fixed schema.
            </p>
          </Section>

          <Section id="agents" title="Notes for agents and pipelines">
            <ul className="list-disc space-y-1 pl-5">
              <li>Start from <Code>GET /v1/queries</Code>; it is generated from each collection&apos;s release, so it never drifts from what runs.</li>
              <li>Resolve user words to values with the dimensions call before running a query; a value a release lacks returns no rows.</li>
              <li>Keep the claim id with every value you use, and cite it.</li>
              <li>When no template fits, write the question in GraphQL with <Code>rows</Code> (step 6), using the parts listed under <Code>collections</Code> in the catalog.</li>
              <li>A bulk pull: list a dimension&apos;s values and run the template once per value; send <Code>&quot;claims&quot;: []</Code> if you only need the rows.</li>
              <li>Errors are plain: a 400 explains which variable is wrong and lists the template&apos;s parameters; a 404 names what does not exist.</li>
            </ul>
          </Section>

          <footer className="border-t border-border/60 pt-6 text-sm text-muted-foreground">
            REST reference: <a className="underline underline-offset-4" href={`${API}/v1/docs`}>{`${API}/v1/docs`}</a> ·
            GraphQL explorer: <a className="underline underline-offset-4" href={`${API}/v1/graphql`}>{`${API}/v1/graphql`}</a>
          </footer>
        </main>
      </div>
    </div>
  );
}
