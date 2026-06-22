from __future__ import annotations

from typing import Any


def build_prompt(entries: list[dict[str, Any]]) -> str:
    rendered_entries: list[str] = []
    for entry in entries:
        definition_parts: list[str] = []
        for definition in entry["definitions"]:
            lines = [f"    source_id {definition['source_id']}: {definition['text']}"]
            for example in definition.get("examples", [])[:2]:
                lines.append(f"      example: {example}")
            definition_parts.append("\n".join(lines))

        definition_lines = (
            "\n".join(definition_parts)
            if definition_parts
            else "    (no real definitions with source ids)"
        )

        header: list[str] = [
            f"Article {entry['article_id']}",
            f"Lemmas: {', '.join(entry['lemmas'])}",
            f"hgno: {entry.get('hgno', '')}",
        ]
        if pos := entry.get("pos", ""):
            header.append(f"part_of_speech: {pos.lower()}")
        header.extend(
            [
                f"tags: {', '.join(entry.get('tags', []))}",
                f"fixed expression: {str(entry.get('is_expression', False)).lower()}",
                "Definitions:",
                definition_lines,
            ]
        )
        rendered_entries.append("\n".join(header))

    numbered = "\n\n".join(rendered_entries)
    return f"""You are an expert lexicographer translating Norwegian dictionary entries to English.

For each article below, provide:
1. A concise primary English memory hook for this exact article/homograph, based
   on the listed definitions. Do NOT translate only the headword spelling. If
   the definitions describe a rare, technical, idiomatic, or non-obvious
   meaning, use that meaning instead of the common meaning of the word form.
   Derive this field from the definition meanings, not from the lemma text. Be
   especially careful when hgno is 2 or higher: that usually means this is a
   separate homograph, so the common translation of the spelling may be wrong.
   - If one meaning clearly dominates, use it as the primary.
   - If the senses span two or more unrelated semantic poles with no dominant
     one, write a compact dual-label, e.g. "pour (rain) / guzzle" or
     "bleat / pour". Do not pick only one pole and ignore the rest.
2. An English gloss for EACH definition entry in the Definitions list below,
   capturing that specific meaning.
   - If a source_id appears more than once (two explanation elements under the
     same definition node), include one translation entry per occurrence in the
     same order they appear — even when the source_id value is repeated.

Before choosing lemma_primary, mentally translate the definitions first. If
lemma_primary does not fit the listed definition glosses, choose a better
definition-based umbrella phrase. For technical entries, prefer a practical
category such as "nautical fitting", "printing term", or "botanical term" over a
misleading common-word translation.

Return ONLY a JSON object like this:
{{{{
  "14903": {{{{
    "definitions": [
      {{{{"source_id": 2, "translation": "aquatic animal that lives in water and breathes with gills"}}}},
      {{{{"source_id": 5, "translation": "fish as food"}}}}
    ],
    "lemma_primary": "fish"
  }}}}
}}}}

Use the exact source_id values from the prompt. Do not invent source ids.
For each article, write the `definitions` array first, then choose
`lemma_primary` as a short summary of those definition translations.
IMPORTANT: Return ONLY the JSON object, no other text.

Articles to translate:
{numbered}
"""
