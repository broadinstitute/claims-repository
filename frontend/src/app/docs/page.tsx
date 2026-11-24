import type { Metadata } from "next";
import type { ReactNode } from "react";
import { BookOpen, Bot, Compass, Database, Dna, ExternalLink, FileSearch, Fingerprint, Layers, Network, Quote, Ruler, Tags, Terminal, Zap, type LucideIcon } from "lucide-react";
import { AgentPrompt } from "./agent-prompt";

export const metadata: Metadata = {
  title: "PIGEAN results · Beta guide",
  description: "Documentation for the PIGEAN beta of the claims repository: association results, score definitions, source evidence and query examples.",
  robots: { index: true, follow: true },
};

/** The API these docs point at. CLAIMS_DOCS_API_URL overrides it per deployment. */
const API = (process.env.CLAIMS_DOCS_API_URL || "https://api-qa.hugeampkpnbi.org/api/claims").replace(/\/+$/, "");
const DAPPER_DOCS = "https://broadinstitute.github.io/dapper/";
const CLAIMS_REPO = "https://github.com/broadinstitute/claims-repository";
const KPN_TRAITS = "https://broadinstitute.github.io/kpn-data-models/";

// A checked example from the QA API, release 2026.11.2. These are source-reported
// values, not a live preview. Keep the release label alongside the example.
const EXAMPLE_CLAIM = "dapper:Claim.BaMUWCeSQpLVWbYSI1FcKiAAjfC1K5t2";
const EXAMPLE_RELEASE = "2026.11.2";
const SECTIONS = [
  ["what", "Overview"],
  ["example", "What is PIGEAN?"],
  ["why", "Claims and annotations"],
  ["dapper", "Where DAPPER fits"],
  ["principles", "Design principles"],
  ["cite", "Reusing results"],
  ["agent", "AI agent guide"],
  ["retrieve", "Query examples"],
  ["reference", "Reference"],
] as const;


const GRAPHQL_EXAMPLE = `query {
  runQuery(
    collection: "pigean"
    query: "top-genes-with-sets"
    variables: {
      trait: "KPN.TRAIT:0000398"
      library: "mouse_msigdb"
      topGenes: 5
      setsPerGene: 3
    }
    claims: ["measurement", "tier-combined", "tier-direct", "tier-indirect",
             "tier-joint", "tier-marginal", "roster"]
  )
}`;

const AGENT_PROMPT = `Use the PIGEAN beta of the claims repository to retrieve the five genes with the highest combined support for type 2 diabetes, with up to three associated gene sets per gene.

Use an HTTP tool or a terminal with network access to execute the requests.

1. Read the query catalog:
GET ${API}/v1/queries
Inspect the available queries, parameters, defaults and metric definitions. Confirm that pigean/top-genes-with-sets is available.

2. Resolve the trait:
GET ${API}/v1/collections/pigean/dimensions/trait?search=T2D
Use KPN.TRAIT:0000398 (type 2 diabetes), not the separate BMI-adjusted trait. Confirm that mouse_msigdb is an available gene-set library in the catalog.

3. Send this GraphQL operation to:
POST ${API}/v1/graphql
Content-Type: application/json
Encode the operation as a JSON body with a "query" string:

${GRAPHQL_EXAMPLE}

Check the HTTP status and any top-level GraphQL errors. The result is the JSON value at data.runQuery; runQuery does not take a GraphQL field selection.

4. Report:
- A table of gene identifiers, combined, log_bf and prior scores, with each measurement claim ID and resolver URL, and the tier each tier claim states.
- The returned gene sets for each gene and their beta and beta_uncorrected scores. Note any missing sets or response warnings.
- The collection release ID and version, dataset dimensions and bound query parameters.
- A brief explanation of the metric definitions. These scores are method-specific, not probabilities or evidence of therapeutic efficacy.

For the first gene, follow its measurement resolver URL and inspect the evidence and value locators in the query response. Report the source file ID and location of the combined score. Distinguish inspecting these references from verifying the original file bytes.

Use only returned results. If network access is unavailable, or a request fails, report that limitation and the error instead of supplying inferred values. Preserve the response and use its returned release for any follow-up query that should refer to the same release.`;

const PRINCIPLES: { icon: LucideIcon; title: string; text: string }[] = [
  { icon: FileSearch, title: "Traceable evidence", text: "Measurements link to source files and value locators, so a reported value can be checked against its source." },
  { icon: Fingerprint, title: "Content-derived identifiers", text: "Claim IDs depend on the fields defining their identity. The same identified content keeps its ID; revisions to those fields produce a different one." },
  { icon: Zap, title: "Claims generated on request", text: "A claim is generated from its result row when a query first returns it, then stored; its ID resolves from then on. Nothing is generated in advance, so a revised template takes effect without reprocessing every result." },
  { icon: Compass, title: "A discoverable interface", text: "Collections describe their results, claim templates and queries through a common interface, and the query catalog declares the supported questions, parameters, defaults and examples. PIGEAN is the method available in this beta." },
  { icon: Ruler, title: "Explicit score definitions", text: "Metrics retain their method-specific scales and interpretations. Evidence tiers describe a named rule, not the probability that a claim is true." },
  { icon: Tags, title: "Identifiers and aliases", text: "Genes and traits retain identifiers, with aliases and ontology mappings where available. Unmapped source names are not silently equated with other concepts." },
];

function Code({ children }: { children: ReactNode }) {
  return <code className="break-words rounded bg-muted/50 px-1.5 py-0.5 font-mono text-[0.85em]">{children}</code>;
}

function Block({ children, label }: { children: string; label: string }) {
  return (
    <figure className="min-w-0 overflow-hidden rounded-lg border border-border">
      <figcaption className="border-b border-border bg-muted/30 px-4 py-2.5 text-xs font-medium text-muted-foreground">{label}</figcaption>
      <pre tabIndex={0} className="overflow-x-auto bg-card p-4 font-mono text-xs leading-6 sm:text-[13px]"><code>{children}</code></pre>
    </figure>
  );
}

function A({ href, children }: { href: string; children: ReactNode }) {
  return <a href={href} className="font-medium text-teal-800 underline decoration-teal-700/35 underline-offset-4 hover:decoration-current dark:text-teal-300">{children}</a>;
}

function Section({ id, title, icon: Icon, intro, children }: { id: string; title: string; icon?: LucideIcon; intro?: string; children: ReactNode }) {
  return (
    <section id={id} className="scroll-mt-6 border-t border-border/60 pt-8">
      <h2 className="flex items-center gap-2.5 text-xl font-semibold tracking-tight">{Icon && <Icon className="size-[18px] shrink-0 text-teal-700 dark:text-teal-400" aria-hidden />}{title}</h2>
      {intro && <p className="mt-3 text-base leading-7 text-muted-foreground">{intro}</p>}
      <div className="mt-4 space-y-4">{children}</div>
    </section>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm leading-6">
        <thead className="text-muted-foreground">
          <tr>{head.map(h => <th scope="col" key={h} className="border-b border-border py-2 pr-4 text-xs font-medium">{h}</th>)}</tr>
        </thead>
        <tbody>{rows.map((row, i) => (
          <tr key={i} className="border-b border-border/60 last:border-0">
            {row.map((cell, j) => <td key={j} className="py-3 pr-4 align-top">{cell}</td>)}
          </tr>
        ))}</tbody>
      </table>
    </div>
  );
}

function Contents() {
  return (
    <ul className="space-y-2 text-sm leading-5">
      {SECTIONS.map(([id, title]) => <li key={id}><a href={`#${id}`} className="text-muted-foreground hover:text-foreground">{title}</a></li>)}
    </ul>
  );
}

/** A compact example alongside the method explanation. */
function ExampleClaim() {
  return (
    <figure className="rounded-lg border border-teal-700/20 bg-card p-4 dark:border-teal-400/20">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-2 text-xs text-muted-foreground">
        <span className="flex items-center gap-1.5 text-teal-700 dark:text-teal-400"><FileSearch className="size-3.5" aria-hidden />Example result</span><span>PIGEAN release {EXAMPLE_RELEASE}</span>
      </figcaption>
      <p className="mt-2 text-sm font-medium">TCF7L2 and type 2 diabetes</p>
      <dl className="mt-3 grid gap-3 sm:grid-cols-3">
        {[
          ["Combined support", "10.9", "Overall association score"],
          ["Direct support", "5.2", "Genetic association evidence"],
          ["Indirect support", "5.73", "Gene-set evidence"],
        ].map(([label, value, meaning]) => (
          <div key={label} className="rounded-md bg-muted/30 px-3 py-2">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="font-mono text-base tabular-nums">{value}<span className="block font-sans text-xs leading-5 text-muted-foreground">{meaning}</span></dd>
          </div>
        ))}
      </dl>
      <p className="mt-3 text-xs leading-5 text-muted-foreground">Source-reported values from the mouse_msigdb gene-set library. Score definitions and the associated claim are shown below.</p>
      <p className="mt-2 text-xs"><A href="#result">See the returned data and claim</A></p>
    </figure>
  );
}

export default function DocsPage() {
  return (
    <div className="min-h-dvh bg-background text-foreground [&_a]:rounded-sm [&_a]:focus-visible:outline-2 [&_a]:focus-visible:outline-offset-4 [&_a]:focus-visible:outline-sky-600 [&_summary]:focus-visible:outline-2 [&_summary]:focus-visible:outline-offset-4 [&_summary]:focus-visible:outline-sky-600 [&_pre]:focus-visible:outline-2 [&_pre]:focus-visible:outline-offset-[-3px] [&_pre]:focus-visible:outline-sky-600">
      <a href="#what" className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-20 focus:bg-background focus:p-3">Skip to content</a>
      <div className="mx-auto grid max-w-6xl gap-8 px-4 py-8 sm:px-6 sm:py-10 lg:grid-cols-[13rem_minmax(0,1fr)] lg:gap-10">
        <nav aria-label="On this page" className="lg:sticky lg:top-6 lg:self-start">
          <div className="hidden lg:block"><p className="mb-3 text-xs font-medium text-muted-foreground">On this page</p><Contents /></div>
          <details className="border-b border-border/60 pb-3 lg:hidden"><summary className="cursor-pointer text-sm font-medium">On this page</summary><div className="pt-3"><Contents /></div></details>
        </nav>
        <main className="min-w-0 max-w-3xl space-y-8 text-base leading-7 [&_p]:max-w-[68ch]">
          <header id="what" className="scroll-mt-6 space-y-4">
            <p className="flex flex-wrap items-center gap-2 text-xs font-medium text-muted-foreground">
              <Database className="size-3.5 text-teal-700 dark:text-teal-400" aria-hidden />Claims repository
              <span className="rounded border border-teal-700/20 bg-teal-50 px-1.5 py-0.5 text-teal-800 dark:border-teal-400/25 dark:bg-teal-950/40 dark:text-teal-300">Beta</span>
            </p>
            <h1 className="text-3xl font-semibold tracking-tight">PIGEAN results</h1>
            <p>
              This repository provides programmatic access to PIGEAN gene–trait and gene-set–trait association results, including score definitions and references to the source data.
            </p>
            <p>
              <strong className="font-semibold">This is a beta release containing PIGEAN results only.</strong>{" "}
              The underlying model is intended to accommodate other analysis methods. Interfaces and annotations may change during the beta.
            </p>
            <p className="text-muted-foreground">
              Results are represented as <strong className="font-medium text-foreground">claims</strong>: statements linked to scores, analysis context and evidence. The purpose is to let researchers and software, including AI agents, retrieve and cite results without losing those definitions or their provenance.
            </p>
          </header>

            <Section id="example" icon={Dna} title="What PIGEAN measures" intro="PIGEAN is a statistical method for prioritizing genes and gene sets associated with a disease or trait.">
              <p>
                A <strong>trait</strong> is a disease or a measured characteristic, such as type 2 diabetes or cholesterol levels. A <strong>gene set</strong> is a group of genes with a shared annotation, such as a biological pathway or a phenotype observed in a model organism.
              </p>
              <div className="grid gap-4 sm:grid-cols-2">
                <div><h3 className="font-semibold">Direct genetic evidence</h3><p className="mt-2 text-sm leading-6 text-muted-foreground">Evidence from genome-wide association studies (GWAS), which look for genetic variants associated with a trait. PIGEAN uses signals near a gene.</p></div>
                <div><h3 className="font-semibold">Indirect gene-set evidence</h3><p className="mt-2 text-sm leading-6 text-muted-foreground">Evidence contributed by the gene sets a gene belongs to. This can help prioritize genes with limited direct genetic support.</p></div>
              </div>
              <p>
                PIGEAN combines these sources into gene–trait scores and also estimates associations between gene sets and traits.
              </p>
              <ExampleClaim />
              <p className="text-sm text-muted-foreground">
                Association scores do not establish the effect or direction of a therapeutic intervention.
              </p>
              <p className="text-sm text-muted-foreground">
                The <A href="https://a2f.hugeamp.org">A2F portal</A> displays PIGEAN results by gene. See also the <A href="https://cfdeknowledge.org/r/kc_gene_set_browser_source?model=cfde&source=gene_set_list_msigdb_nohp">PIGEAN method overview</A>.
              </p>
            </Section>

            <Section id="why" icon={Quote} title="Claims and annotations">
              <p>
                The REST and GraphQL APIs return both the numerical results and structured annotations. For a PIGEAN gene–trait result, these include:
              </p>
              <Table head={["Record", "Contents"]} rows={[
                ["Statement", "The association being assessed, with identifiers for the gene and trait."],
                ["Scores", "Direct, indirect and combined support, each with a metric name, scale and interpretation."],
                ["Analysis context", "The method, dataset and gene-set library. The query response also identifies the collection release."],
                ["Evidence", "References to source files and locators for the reported values within them."],
                ["Claim identifier", "A content-derived ID and a URL for retrieving the claim."],
              ]} />
              <p>
                “Self-annotating” means that declared templates attach these definitions and evidence references to source results. The annotations describe the analysis; they do not add new biological inferences. A claim is generated when a query first returns its result and is stored from then on, so its ID and resolver URL work for anyone you share them with. Take claim IDs from query responses; an ID is not resolvable before its result has been returned. A machine-readable query catalog describes the available queries, parameters and metrics so software can inspect them before requesting results.
              </p>
            </Section>

            <Section id="dapper" icon={Network} title="DAPPER and the repository">
              <p>
                <A href={DAPPER_DOCS}>DAPPER</A> is the data model used to represent claims, scores, evidence and provenance—the record of where a result came from and how it was produced. PIGEAN produces the analysis results; DAPPER describes them; the claims repository makes those records queryable.
              </p>
              <p>
                DAPPER identifiers are computed from the fields that define a record&apos;s identity. Those fields determine whether two records have the same ID. Retaining the claim ID, collection release and query parameters distinguishes a retrieved result from subsequent revisions.
              </p>
              <details id="words" className="scroll-mt-8 rounded-lg border border-border/70 p-4">
                <summary className="cursor-pointer font-medium">API terminology</summary>
                <dl className="mt-4 space-y-4 text-sm leading-6">
                  {[
                    ["Collection", "A named group of results, with its data, claim templates and queries. The collection used here is pigean."],
                    ["Release", "A version of that collection. Keep the returned release ID and version with imported results."],
                    ["Proposition", "The statement being assessed, such as an association between a gene and a trait."],
                    ["Claim", "A particular assessment of that statement, linked to its scores, method and evidence. Generated from a result row the first time a query returns it; its ID resolves from then on."],
                    ["Provenance", "The record of where a result came from and how it was produced."],
                    ["Value locator", "A reference to the exact serialized value within a source file, including its byte position."],
                  ].map(([term, definition]) => <div key={term}><dt className="font-semibold">{term}</dt><dd className="text-muted-foreground">{definition}</dd></div>)}
                </dl>
              </details>
            </Section>

            <Section id="principles" title="Design principles" icon={Compass}>
              <div className="grid gap-x-6 gap-y-5 sm:grid-cols-2">
                {PRINCIPLES.map(({ icon: Icon, title, text }) => (
                  <div key={title} className="flex items-start gap-3">
                    <Icon className="mt-1 size-4 shrink-0 text-teal-700 dark:text-teal-400" aria-hidden />
                    <div><h3 className="text-sm font-medium">{title}</h3><p className="mt-1 text-sm leading-6 text-muted-foreground">{text}</p></div>
                  </div>
                ))}
              </div>
            </Section>

            <Section id="cite" icon={Layers} title="Reusing results">
              <p>When importing results into a target-discovery platform or another analysis:</p>
              <ul className="list-disc space-y-3 pl-5 text-sm leading-6">
                <li>Map gene and trait identifiers to your system&apos;s entities. The <A href={KPN_TRAITS}>Knowledge Portal Network (KPN) trait registry</A> supplies definitions and ontology mappings where available; some traits retain source names.</li>
                <li>Retain metric definitions, the claim ID and resolver URL, dataset dimensions, collection release, query parameters and any warnings.</li>
                <li>Keep the original response. Specify the returned release ID or version when repeating a query against the same release.</li>
                <li>Use the claim&apos;s evidence references to inspect its source. File records contain checksums; value locators identify the reported numbers&apos; positions within those files.</li>
              </ul>
              <p className="text-sm text-muted-foreground">The same records can accompany an AI agent&apos;s answer: claim IDs identify the cited assessments, and the release and parameters record what was queried.</p>
            </Section>

            <Section id="agent" title="AI agent guide" icon={Bot}>
              <p>
                Give the prompt below to an agent with an HTTP tool or terminal access. It uses the query catalog to check the available parameters, then retrieves results through GraphQL. A chat interface that cannot make HTTP requests will need an appropriate tool connection.
              </p>
              <AgentPrompt prompt={AGENT_PROMPT} />
              <p className="text-sm text-muted-foreground">
                To use another disease, replace the question and resolve its trait identifier before querying. For follow-up analyses, specify the release returned by the first request. The <A href="#own">GraphQL example below</A> shows the operation separately.
              </p>
            </Section>

            <Section id="retrieve" icon={Terminal} title="Query examples" intro="The following example retrieves genes ranked by PIGEAN combined support for type 2 diabetes, with associated gene sets.">
              <p className="text-sm text-muted-foreground">These examples use the beta API at <span className="break-all font-mono text-xs">{API}</span>. You can inspect the catalog in a browser or run the commands in a terminal.</p>
              <div className="space-y-3">
                <h3 className="text-base font-semibold">1. List available queries</h3>
                <p className="text-sm text-muted-foreground">The catalog lists the queries supported by this deployment, their parameters, defaults, score definitions and example requests.</p>
                <Block label="Read the query catalog">{`curl '${API}/v1/queries'`}</Block>
                <p className="text-sm"><A href={`${API}/v1/queries`}>Open the query catalog</A></p>
              </div>
              <div className="space-y-3">
                <h3 className="text-base font-semibold">2. Look up the trait</h3>
                <p className="text-sm text-muted-foreground">Searching for T2D returns several related traits. Choose <Code>KPN.TRAIT:0000398</Code> for type 2 diabetes. The alias <Code>T2D</Code> is also accepted. Type 2 diabetes adjusted for body mass index is a separate trait, <Code>T2DadjBMI</Code>.</p>
                <Block label="Search the collection’s trait vocabulary">{`curl '${API}/v1/collections/pigean/dimensions/trait?search=T2D'`}</Block>
              </div>
              <div className="space-y-3">
                <h3 className="text-base font-semibold">3. Retrieve results and claims</h3>
                <p className="text-sm text-muted-foreground">Request five genes ranked by combined support, with up to three associated gene sets per gene. Here we request every claim the results carry (listed below); leaving out <Code>claims</Code> returns the same. The response also identifies the collection release used.</p>
                <Block label="Run the top-genes-with-sets query">{`curl -X POST '${API}/v1/collections/pigean/queries/top-genes-with-sets:run' \\
  -H 'Content-Type: application/json' \\
  -d '{
    "variables": {
      "trait": "KPN.TRAIT:0000398",
      "library": "mouse_msigdb",
      "topGenes": 5,
      "setsPerGene": 3
    },
    "claims": ["measurement", "tier-combined", "tier-direct", "tier-indirect",
               "tier-joint", "tier-marginal", "roster"]
  }'`}</Block>
              </div>
              <div id="result" className="scroll-mt-8 space-y-4">
                <h3 className="text-base font-semibold">Response and score definitions</h3>
                <p className="text-sm text-muted-foreground">The example above is TCF7L2 from release {EXAMPLE_RELEASE}, using the <Code>mouse_msigdb</Code> gene-set library. The numbers below are reported source values; the current release may change.</p>
                <Block label={`Selected fields from one returned row · release ${EXAMPLE_RELEASE}`}>{JSON.stringify({
                  subject: { id: "HGNC.SYMBOL:TCF7L2", value: "TCF7L2" },
                  object: { id: "KPN.TRAIT:0000398", label: "Type 2 diabetes (T2D)" },
                  metrics: { combined: 10.9, log_bf: 5.2, prior: 5.73 },
                  claims: {
                    measurement: { id: EXAMPLE_CLAIM, resolverUrl: `${API}/v1/objects/${EXAMPLE_CLAIM}` },
                    "tier-combined": { id: "dapper:Claim.yBVEz1OdMGE4f3duBUNLzs4suVziwA7a" },
                    "tier-direct": { id: "dapper:Claim.T8ox4qVmTtZHhTgFT74GNnhE1Fj1RwNu" },
                    "tier-indirect": { id: "dapper:Claim.-JuFBKptdwGgTibyYmrVSjyUdorUHM0U" },
                  },
                }, null, 2)}</Block>
                <p className="text-sm text-muted-foreground">This excerpt omits the full claim envelopes and the associated gene sets. In the full response each claim also includes its statement, scores with their interpretations, evidence and source references. Joined gene-set results appear under <Code>sets</Code>, each with its own claims and the roster claim on its edge.</p>
                <h3 className="pt-2 text-base font-semibold">The claims each result carries</h3>
                <p className="text-sm text-muted-foreground">A measurement claim states the reported numbers, with evidence pointing to each one in its source file. A tier claim reads one score against a named rule and states the tier it falls in; it adds no data beyond the score and the rule, and the rule is the collection&apos;s, not a probability. The statements below are TCF7L2&apos;s for type 2 diabetes, and those of mp_polyphagia, one of its gene sets.</p>
                <Table head={["Claim key", "For", "What it states"]} rows={[
                  [<span key="m" className="whitespace-nowrap"><Code>measurement</Code></span>, "gene", "PIGEAN assessed the association of TCF7L2 with type 2 diabetes (combined 10.9; log BF 5.2; prior 5.73)."],
                  [<span key="tc" className="whitespace-nowrap"><Code>tier-combined</Code></span>, "gene", "TCF7L2 is relevant to type 2 diabetes with very strong evidence, because its combined score is 10.9."],
                  [<span key="td" className="whitespace-nowrap"><Code>tier-direct</Code></span>, "gene", "… genetically relevant … with very strong evidence, because its direct (log Bayes factor) score is 5.2."],
                  [<span key="ti" className="whitespace-nowrap"><Code>tier-indirect</Code></span>, "gene", "… functionally relevant … with very strong evidence, because its indirect (gene-set prior) score is 5.73."],
                  [<span key="ms" className="whitespace-nowrap"><Code>measurement</Code></span>, "gene set", "PIGEAN assessed the association of gene set mp_polyphagia with type 2 diabetes (beta 3.06e-05; beta_uncorrected 2.25)."],
                  [<span key="tj" className="whitespace-nowrap"><Code>tier-joint</Code></span>, "gene set", "mp_polyphagia is relevant to type 2 diabetes with not significant evidence, because its joint effect (beta) is 3.06e-05."],
                  [<span key="tm" className="whitespace-nowrap"><Code>tier-marginal</Code></span>, "gene set", "… with extremely significant evidence, because its marginal effect (beta_uncorrected) is 2.25."],
                  [<span key="r" className="whitespace-nowrap"><Code>roster</Code></span>, "gene set membership", "The mouse_msigdb library defines the membership of mp_polyphagia: 76 genes."],
                ]} />
                <p className="text-sm text-muted-foreground">Gene tiers: very strong at 3 or more, strongly suggestive at 2 or more, nominally significant at 1 or more, otherwise not significant. Gene-set tiers: extremely significant at 1 or more, strongly significant at 0.1 or more, significant at 0.01 or more, otherwise not significant.</p>
                <Table head={["Metric", "How to read it"]} rows={[
                  [<Code key="combined">combined</Code>, "Combined gene–trait support, on a log posterior odds scale. The query uses it to rank genes; higher means stronger support under PIGEAN."],
                  [<Code key="log_bf">log_bf</Code>, "Direct genetic support, on a log Bayes factor scale. Keep it separate from evidence contributed by gene sets."],
                  [<Code key="prior">prior</Code>, "Indirect support from gene-set membership, on a log prior odds scale. It can be negative."],
                  [<Code key="beta">beta</Code>, "For a gene set: the effect estimated jointly with other selected gene sets."],
                  [<Code key="beta_uncorrected">beta_uncorrected</Code>, "For a gene set: its marginal effect, without adjustment for the other gene sets."],
                ]} />
                <p className="text-sm text-muted-foreground">These are method-specific, log-scale quantities, not probabilities.</p>
                <p className="text-sm"><A href={`${API}/v1/objects/${EXAMPLE_CLAIM}`}>Inspect the example&apos;s full claim</A></p>
              </div>
              <details className="rounded-lg border border-border/70 p-4">
                <summary className="cursor-pointer font-medium">Repeat the example against a named release</summary>
                <div className="mt-4 space-y-3">
                  <p className="text-sm text-muted-foreground">Add <Code>release</Code> beside <Code>variables</Code> in the request body. For your own integration, use the release returned by your first query.</p>
                  <Block label="Request body pinned to the example release">{JSON.stringify({ release: EXAMPLE_RELEASE, variables: { trait: "T2D", library: "mouse_msigdb", topGenes: 5, setsPerGene: 3 }, claims: ["measurement", "tier-combined", "tier-direct", "tier-indirect", "tier-joint", "tier-marginal", "roster"] }, null, 2)}</Block>
                </div>
              </details>
              <details id="own" className="scroll-mt-8 rounded-lg border border-border/70 p-4">
                <summary className="cursor-pointer font-medium">Use GraphQL for the same query</summary>
                <div className="mt-4 space-y-3">
                  <p className="text-sm text-muted-foreground">GraphQL is another way to query the service. The explorer is a browser-based tool for writing and running requests; its examples also show how to build more specific questions.</p>
                  <Block label={`POST ${API}/v1/graphql`}>{GRAPHQL_EXAMPLE}</Block>
                  <p className="text-sm"><A href={`${API}/v1/graphql`}>Open the GraphQL explorer</A></p>
                </div>
              </details>
            </Section>

            <Section id="reference" icon={BookOpen} title="Reference">
              <div className="divide-y divide-border">
                {[
                  [`${API}/v1/queries`, "Query catalog", "Available queries, parameters, metric definitions and examples."],
                  [`${API}/v1/docs`, "REST API reference", "The technical reference for web requests and response formats."],
                  [`${API}/v1/graphql`, "GraphQL explorer", "Browser-based query editor, schema and examples."],
                  [KPN_TRAITS, "KPN trait registry", "Trait identifiers, aliases and ontology mappings."],
                  [DAPPER_DOCS, "DAPPER documentation", "Definitions of claims, scores, source files and provenance."],
                  [CLAIMS_REPO, "Repository source code", "Implementation and technical documentation on GitHub; repository access may be required."],
                ].map(([href, title, description]) => (
                  <div key={title} className="py-4 first:pt-0"><a href={href} className="inline-flex items-center gap-2 font-semibold underline decoration-current/30 underline-offset-4">{title}<ExternalLink className="size-3.5" aria-hidden /></a><p className="mt-1 text-sm leading-6 text-muted-foreground">{description}</p></div>
                ))}
              </div>

            </Section>
          <footer className="flex flex-wrap justify-between gap-3 border-t border-border/60 pt-5 text-xs text-muted-foreground"><span>Claims repository</span><span>Beta release — PIGEAN results only</span></footer>
        </main>
      </div>
    </div>
  );
}
