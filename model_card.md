# DocuBot Model Card

This model card is a short reflection on my DocuBot system. I filled it out after implementing retrieval and testing the same questions in all three modes:

1. Naive LLM over full docs  
2. Retrieval only  
3. RAG (retrieval plus LLM)

All results below come from runs with `gemini-3.5-flash-lite`. The starter model name, `gemini-flash-lite-latest`, returned a 404 error for my API key, and `gemini-2.5-flash-lite` was no longer available to new users, so I changed `GEMINI_MODEL_NAME` in `llm_client.py`. I also added a small retry (1s, 2s, 4s) for temporary 503 "high demand" errors, because those errors were wiping out whole test runs.

---

## 1. System Overview

**What is DocuBot trying to do?**  

> DocuBot answers developer questions about a codebase by using the project's own documentation in the `docs/` folder. The goal is to give answers that are backed by the docs, and to say "I do not know" when the docs do not support an answer. It lets you compare three approaches so you can see how much grounding changes the quality of the answer.

**What inputs does DocuBot take?**  

> DocuBot takes a developer question (typed in, or the built-in sample queries from `dataset.py`), the `.md` and `.txt` files in `docs/`, and the `GEMINI_API_KEY` environment variable from `.env`. Modes 1 and 3 need the API key; Mode 2 does not.

**What outputs does DocuBot produce?**

> Mode 1 produces a free-form answer from Gemini. Mode 2 produces the raw doc sections it retrieved, each labeled with its filename, or the refusal "I do not know based on these docs." Mode 3 produces a short Gemini answer written only from the retrieved sections, with the files it relied on, or a refusal.

---

## 2. Retrieval Design

**How does your retrieval system work?**  

> **Unit of retrieval.** My first version scored whole documents. It returned entire files, and for almost every question every file scored above zero, so the useful sentence was buried in hundreds of lines. I changed the unit of retrieval to markdown sections, splitting each file every time a line starts with `#`, `##`, or `###`. I chose headings instead of blank-line paragraphs because paragraph splitting would separate `### GET /api/users` from the sentence "Returns a list of all users." under it, and then the snippet would not say which endpoint it was. Sections that are only a heading with no body are dropped. This gives 36 sections of about 200 to 500 characters each.
>
> **Tokenizing.** Text is lowercased and split on anything that is not a letter or digit, so `generate_access_token` becomes `generate`, `access`, `token` and `/api/users` becomes `api`, `users`. Question words such as "how", "where", "which", "the", "docs", and "mention" are removed as stopwords. A very small stemmer strips endings like "ing", "ion", "ed", "es", and "s" so that "generated", "generate", and "generation" all match.
>
> **Index.** `build_index` maps each stemmed word to the list of section ids that contain it. `retrieve` uses the index to collect only the sections that share at least one word with the question.
>
> **Scoring.** `score_document` adds three parts. Coverage is the number of distinct question words found in the section. Heading bonus is +1 for each question word that also appears in the section's heading. Exact-term bonus is +2 when a code-like term from the question, meaning anything with `/` or `_` such as `/api/projects/<project_id>`, appears word for word in the section.
>
> **Choosing top snippets.** Sections are sorted by score, highest first, with ties kept in document order, and the top 3 are returned.

**What tradeoffs did you make?**  

> I chose simplicity over accuracy. Keyword matching is fast and easy to reason about line by line, but it only works when the question uses the same words as the docs. I added the heading bonus and exact-term bonus after I saw the plain coverage score rank `POST /api/login` above `GET /api/users` for "Which endpoint returns all users?" (the login section matched "username" and "returns"), and rank the general `/api/projects` section above the exact `/api/projects/<project_id>` section. Counting distinct words instead of total occurrences means a long section that repeats "token" many times does not automatically win. The cost is that two sections often tie, so ranking depends on small bonuses. Section size also depends on how the doc author used headings, so some sections, like the short "# Database Guide" intro, carry almost no facts but still match well.

---

## 3. Use of the LLM (Gemini)

**When does DocuBot call the LLM and when does it not?**  

- Naive LLM mode: always calls Gemini, once per question.
- Retrieval only mode: never calls Gemini.
- RAG mode: calls Gemini only if retrieval found at least one section that passes the guardrail. If retrieval returns nothing, DocuBot refuses without calling the LLM at all.

> One important detail is that the starter's `naive_answer_over_full_docs` ignores the `all_text` argument, so in Mode 1 the model never actually sees the docs. It only sees "You are a documentation assistant. Answer this developer question." I left this as is, because it shows what happens when a model answers from general knowledge alone, but it means Mode 1 is really "no context at all" rather than "full docs."

**What instructions do you give the LLM to keep it grounded?**  

> The RAG prompt shows each retrieved section labeled with its filename, then tells the model to answer using only the information in the snippets, not to invent functions, endpoints, or configuration values, to reply exactly "I do not know based on the docs I have." if the snippets are not enough, and to mention which files it relied on.

---

## 4. Experiments and Comparisons

I ran the eight sample queries from `dataset.py`, plus "Which endpoint returns all users?" and one off-topic question, through `python main.py` in all three modes with identical wording.

| Query | Naive LLM: helpful or harmful? | Retrieval only: helpful or harmful? | RAG: helpful or harmful? | Notes |
|------|---------------------------------|--------------------------------------|---------------------------|-------|
| Where is the auth token generated? | Unhelpful. Asked me for more context. | Helpful. Top section is "## Token Generation" with `generate_access_token` in `auth_utils.py`. | Helpful. Named `generate_access_token` in `auth_utils.py`, cited AUTH.md. | Mode 1 cannot answer because it never sees the docs. |
| What environment variables are required for authentication? | Harmful. Invented `AUTH_CLIENT_ID` and `AUTH_CLIENT_SECRET`. | Correct but hard to read. Top section is SETUP's "Required Variables", which leads with `DATABASE_URL`. | Mostly helpful. Listed `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS`. | RAG did not mention that SETUP.md calls `TOKEN_LIFETIME_SECONDS` optional while AUTH.md says both are required. |
| How do I connect to the database? | Harmful. Invented `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` and suggested Prisma or Knex. | Helpful. Returned "Connection Configuration" with `DATABASE_URL` examples. | Very helpful. `DATABASE_URL`, SQLite default `app.db`, PostgreSQL format, and `createdb appdb`. | The docs use one `DATABASE_URL`, not separate variables. |
| Which endpoint lists all users? | Unhelpful. Asked for context and guessed `GET /api/v1/users`. | Helpful. Top section is "### GET /api/users". | Helpful but incomplete. Said `GET /api/users`. | RAG left out "Only accessible to admins" even though it was in the snippet. |
| Which endpoint returns all users? | Unhelpful. Asked for the API docs. | Helpful after the heading bonus. Before it, `POST /api/login` ranked first. | Helpful. `GET /api/users`, cited API_REFERENCE.md. | Same question, different wording, same result after the scoring fix. |
| What does the /api/projects/<project_id> route return? | Harmful. Described "standard REST conventions" with `created_at` and team members. | Helpful after the exact-term bonus. | Helpful. Listed `project_id`, `name`, `description`, `status`, `owner`. | Naive mode invented fields that are not in the docs. |
| Is there any mention of payment processing in these docs? | Unhelpful. Said I forgot to include the docs. | Correct refusal. | Correct refusal. | The guardrail refused before calling the LLM. |
| How does a client refresh an access token? | Harmful. Described an OAuth 2.0 `refresh_token` grant at `/oauth/token`. | Helpful. "Client Workflow" and "POST /api/refresh". | Helpful. `POST /api/refresh` with the current token as `Authorization: Bearer <token>`. | The docs exchange the existing token; there is no separate refresh token. |
| Which fields are stored in the users table? | Harmful. Listed Laravel-style fields such as `remember_token` and `email_verified_at`. | Helpful. Returned the "### users" table. | Helpful. `user_id`, `email`, `password_hash`, `joined_at`. | Very confident, very wrong in Mode 1. |
| How do I deploy this app to Kubernetes? | Harmful. Wrote a full Dockerfile, Deployment YAML, and Helm guide for a Node app. | Correct refusal. | Correct refusal. | The docs say nothing about Kubernetes. |

**What patterns did you notice?**  

- **When does naive LLM look impressive but untrustworthy?** Naive mode looked the most impressive on questions that sound like common patterns, such as refreshing tokens, user tables, and Kubernetes deployment. It wrote long, well-formatted answers with code blocks, but the details came from general conventions (OAuth, Laravel, Node) and not from this project. The users table answer is the clearest example; it listed eight fields with confidence, and only one of them (`email`) is actually in DATABASE.md. This shows that fluent output is not evidence. When the question was very specific to the project, like "Where is the auth token generated?", the model asked for context instead, which is at least honest.
- **When is retrieval only clearly better?** Retrieval only is clearly better than naive mode whenever accuracy matters, because everything it shows is text that really exists in the docs, and it refuses off-topic questions instead of inventing an answer. It is the only mode where you can check every word yourself. However, it is hard to interpret. For "What environment variables are required for authentication?" it returned three sections, and the top one starts with `DATABASE_URL`, which is not an auth variable, while the second is just the AUTH.md intro paragraph that says it "covers... required environment variables" without listing any. You have to read all three and put the answer together yourself.
- **When is RAG clearly better than both?** RAG was better than both on "How do I connect to the database?" It combined the `DATABASE_URL` examples from DATABASE.md with the `createdb appdb` step from SETUP.md into one short answer and cited both files. It had the accuracy of retrieval only and the readability of naive mode. RAG also refused correctly on some questions that slipped past my retrieval guardrail, such as "How do I connect to the payment database?" and "How do I reset a user's password?", where retrieval returned loosely related sections but the model saw they did not answer the question.

---

## 5. Failure Cases and Guardrails

**Describe at least two concrete failure cases you observed.**  

> **Failure case 1: false refusal from a vocabulary mismatch (RAG).** I asked "How do I log in?" The docs clearly answer this with `POST /api/login`. My tokenizer turns "log in" into the word `log`, but the docs use `login`, so the only section that matched was "Common Failure Cases" ("instruct users to log in again"). RAG then correctly said "I do not know based on the docs I have." because that snippet did not answer the question. What should have happened is that retrieval found the `/api/login` section. The same thing happened with "What port does the server run on?": my stemmer turns "running" into `runn`, which does not match `run`, so the "## 4. Running the Application" section that says port 5000 was never retrieved. This shows that RAG can only be as good as its retrieval; the LLM refused correctly, but the user still did not get an answer that was in the docs.

> **Failure case 2: confident but incomplete answer (RAG).** I asked "When does a token expire?" Retrieval returned "Token Generation", "GET /api/users/<user_id>", and "Validating Requests", but not "Environment Variables". RAG answered that a token expires at the time in its `expires_at` field and returns 401 after that. That is true, but it left out the most useful fact, which is that `TOKEN_LIFETIME_SECONDS` controls the lifetime and defaults to 3600 seconds. What should have happened is that the Environment Variables section ranked in the top 3, or the model said its answer might be incomplete. A related case is "Which endpoint lists all users?", where RAG answered `GET /api/users` but dropped "Only accessible to admins" even though that sentence was in the snippet it was given.

> **Failure case 3: conflicting sources blended silently (RAG).** For "What environment variables are required for authentication?" AUTH.md says both `AUTH_SECRET_KEY` and `TOKEN_LIFETIME_SECONDS` "must be configured," while SETUP.md lists `TOKEN_LIFETIME_SECONDS` under Optional Variables. RAG only reported the AUTH.md version and did not point out that the docs disagree.

**When should DocuBot say "I do not know based on the docs I have"?**  

> DocuBot should refuse when the question is about a topic the docs never mention, such as payment processing or Kubernetes deployment. It should also refuse when the docs mention the words in the question but do not actually answer it, such as "How do I reset a user's password?", where the docs mention `password_hash` but never describe a reset flow. Finally, it should refuse, or at least warn, when the question mixes a real topic with a made-up detail, such as "the payment database," because answering about the normal database would be misleading.

**What guardrails did you implement?**  

> The main guardrail is in `retrieve()`, so both Mode 2 and Mode 3 use it. A section only counts as evidence if it contains at least half (`MIN_MATCH_FRACTION = 0.5`) of the meaningful words in the question. This check uses plain word coverage, not the final score, so the heading and exact-term bonuses can only reorder evidence; they can never turn weak evidence into enough evidence. If no section passes, `retrieve()` returns an empty list, and both answer modes return "I do not know based on these docs." without calling the LLM. Stopwords are removed first so that words like "how" or "docs" cannot satisfy the guardrail by themselves. On top of that, the RAG prompt has its own refusal rule, which caught "payment database" and "reset a user's password" after they passed the retrieval guardrail. Retrieval is also limited to the top 3 sections so the LLM receives only a small amount of context.

---

## 6. Limitations and Future Improvements

**Current limitations**  

1. Retrieval only matches exact word stems, so synonyms and different phrasing fail ("log in" vs "login", "expire" vs "lifetime", "run" vs "running"). This causes false refusals for questions the docs do answer.
2. The 50% coverage guardrail can be fooled by questions that share most of their words with the docs but add one made-up detail ("payment database"). In Mode 2 those questions return confident-looking but irrelevant sections.
3. Sections depend on how the docs use headings. Intro sections like "# Database Guide" have almost no facts but still rank highly, and a long section can still contain a lot of text unrelated to the question.
4. Naive mode (Mode 1) does not send the docs to the model at all, so it is not a fair "full docs" baseline.
5. Nothing detects when two docs disagree, so RAG can present one side of a conflict as the whole answer.

**Future improvements**  

1. Improve matching: a better stemmer (handle doubled consonants like "running"), a small synonym list ("log in" to "login", "expire" to "lifetime"), and weighting rare words higher than common ones like "api" or "token".
2. Prepend each section's parent heading and filename to its text (for example "DATABASE.md > Tables > users") so short subsections carry their context, and skip intro sections that only describe the document.
3. Tell the RAG prompt to mention restrictions (like admin-only access) and to point out when two files disagree, and show the retrieved snippets alongside the RAG answer so the user can verify it.

---

## 7. Responsible Use

**Where could this system cause real world harm if used carelessly?**  

> The biggest risk is a developer trusting a fluent answer without checking it. Naive mode invented environment variables, database fields, and an OAuth flow that do not exist in this project; someone following that answer could misconfigure authentication or write code against fields that are not there. Even RAG can leave out important details, such as the fact that `GET /api/users` is admin-only, which could lead a developer to expose user data or misunderstand access rules. A false refusal is less dangerous, but it can make someone think the docs do not cover something they actually do.

**What instructions would you give real developers who want to use DocuBot safely?**  

- Treat every answer as a starting point and open the cited file to confirm it, especially for anything about authentication, permissions, or secrets.
- Do not use naive mode for project-specific questions; it answers from general knowledge and will sound confident even when it is wrong.
- If DocuBot says "I do not know," try rephrasing with the words the docs are likely to use (for example "login" instead of "log in") before assuming the docs do not cover it.
- Keep the docs up to date and consistent with each other, because DocuBot can only be as accurate as the files it reads.
