# URL and Slug Style Guide

Slugs are the human-readable part of a URL. Every public page, article and product uses one.

## Rules for slugs

- Slugs are lowercase ASCII only. Accented characters are transliterated to their closest ASCII letter (é becomes e, ü becomes u) and any other non-ASCII character is dropped.
- Words are separated by a single hyphen. Runs of spaces, underscores or punctuation collapse into one hyphen, and a slug never contains two hyphens in a row.
- Slugs never start or end with a hyphen.
- Slugs are at most 60 characters. When truncating, cut at the last hyphen boundary so no word is split.
- An input that produces an empty slug (for example only punctuation) must return the fallback slug "untitled".

## Examples

| Title | Slug |
|---|---|
| Hello, World! | hello-world |
| Crème Brûlée Recipe | creme-brulee-recipe |
| --Already--Slugged-- | already-slugged |
