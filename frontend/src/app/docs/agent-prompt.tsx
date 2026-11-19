"use client";

import { useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

export function AgentPrompt({ prompt }: { prompt: string }) {
  const field = useRef<HTMLTextAreaElement>(null);
  const [status, setStatus] = useState<"idle" | "copied" | "failed">("idle");

  async function copyPrompt() {
    try {
      await navigator.clipboard.writeText(prompt);
      setStatus("copied");
    } catch {
      field.current?.focus();
      field.current?.select();
      setStatus("failed");
    }
  }

  return (
    <div className="overflow-hidden rounded-lg border border-teal-700/20 dark:border-teal-400/20">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-teal-700/15 bg-teal-50/70 px-4 py-2 dark:border-teal-400/15 dark:bg-teal-950/25">
        <label htmlFor="agent-starter-prompt" className="text-xs font-medium text-teal-900 dark:text-teal-200">Starter prompt: type 2 diabetes</label>
        <button type="button" onClick={copyPrompt} className="inline-flex items-center gap-1.5 rounded px-2 py-1 text-xs font-medium text-teal-800 hover:bg-teal-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal-700 dark:text-teal-200 dark:hover:bg-teal-900/40 dark:focus-visible:outline-teal-300">
          {status === "copied" ? <Check className="size-3.5" aria-hidden /> : <Copy className="size-3.5" aria-hidden />}
          {status === "copied" ? "Copied" : "Copy prompt"}
        </button>
      </div>
      <textarea id="agent-starter-prompt" ref={field} readOnly value={prompt} rows={18} spellCheck={false} aria-describedby="agent-prompt-status" className="block w-full resize-y bg-card p-4 font-mono text-xs leading-6 text-foreground focus-visible:outline-2 focus-visible:outline-offset-[-3px] focus-visible:outline-teal-700 dark:focus-visible:outline-teal-300" />
      <p id="agent-prompt-status" role="status" className="border-t border-border/60 px-4 py-2 text-xs leading-5 text-muted-foreground">
        {status === "copied" ? "Prompt copied to clipboard." : status === "failed" ? "Clipboard access is unavailable. The prompt is selected; use your browser’s copy command." : "Copy the full prompt, or select and copy the text."}
      </p>
    </div>
  );
}
