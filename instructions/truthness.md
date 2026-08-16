# Truthness Ranking Instructions

Evaluate the trustworthiness, credibility, and empirical grounding of the document's content. Assign a score from 1 (lowest) to 10 (highest) and provide a concise justification.

## Scoring Rubric

- **9-10: Highly Trustworthy / Empirical**
  - Peer-reviewed research papers, official model specifications, or primary developer documentation with direct code examples, math derivations, or replication results. Highly objective and precise.

- **7-8: Trustworthy / Expert Analysis**
  - Well-written technical blog posts (e.g., from notable AI engineers/labs), rigorous benchmarks with documented methodology, or deep technical reviews containing clear data-driven claims.

- **5-6: Moderately Trustworthy / Informational**
  - General introductory guides, news articles, opinion posts, or synthesis documents that describe technologies without detailed evidence, empirical tests, or direct code references.

- **3-4: Low Trustworthiness / Speculative**
  - Documents containing speculative claims, promotional content/PR, heavily opinionated discussions without technical depth, or outdated claims lacking context.

- **1-2: Untrustworthy / Hallucinatory or Spam**
  - Spam, automatically generated text, documents with obvious factual errors, or content that contains contradictory statements without clarification.

## Output Format
Return a JSON object with the following fields:
```json
{
  "score": <integer from 1 to 10>,
  "justification": "<concise explanation of why this score was assigned>"
}
```
Ensure the response is valid JSON and contains only the JSON object.
