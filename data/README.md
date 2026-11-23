# Bitext sample and priority review

The upstream CSV contains 26,872 synthetic support exchanges. The review queue samples 24 normalized unique instructions from each of 11 categories and splits them 12/12 between dev and test. Lowercase words, remove punctuation and collapse whitespace before hashing, deduplicating globally and sorting per category. Source row numbers count from 1 after the header. This is category-balanced and does not estimate customer traffic mix.

Bitext supplies category and a reference reply. Product is our versioned operational mapping in `config/catalog.json`. It identifies a support product area. Priority rubric v1: urgent = explicit ongoing account compromise or financial fraud, high = explicitly blocked access/payment or duplicate charge, normal = ordinary action or problem, low = informational request or feedback. Resolve ambiguous cases as normal and document the rationale. Never infer urgency from spelling alone.

The published subset and derivative review fields are available under [CDLA-Sharing-1.0](https://cdla.dev/sharing-1-0/). Original publisher [Bitext](https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset/tree/430d1a89bd93bd1fa23c16f29dd53e73f0087443). Changes include sampling, normalization for deduplication, derived identifiers, product and split assignment. The original CSV remains local. Data license applies to the subset independently of the brief.

Reviewers fill `priority,reviewer,rationale` in `data/reviews.csv` after reading each corresponding ticket. Run `python -m scripts.dataset validate` to require at least 200 unique completed reviews. Preserve the split once frozen. Regeneration preserves existing review records.
