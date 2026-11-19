"""
Core DocuBot class responsible for:
- Loading documents from the docs/ folder
- Building a simple retrieval index (Phase 1)
- Retrieving relevant snippets (Phase 1)
- Supporting retrieval only answers
- Supporting RAG answers when paired with Gemini (Phase 2)
"""

import os
import glob
import re

# Question words that carry no topic meaning. Matching on these would make
# almost every section look "relevant", so we drop them before scoring.
STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "be", "of", "to", "in", "on", "for",
    "and", "or", "with", "by", "at", "from", "as", "it", "this", "that",
    "these", "those", "there", "any", "do", "does", "did", "how", "what",
    "which", "where", "when", "who", "why", "i", "my", "we", "you", "your",
    "can", "should", "mention", "docs", "doc", "documentation",
}

# Guardrail: a section only counts as evidence if it contains at least this
# fraction of the meaningful words in the question.
MIN_MATCH_FRACTION = 0.5


def stem(word):
    """
    Very small suffix stripper so that word variants match each other:
    generated / generate / generation -> "generat", users -> "user",
    tables -> "tabl". Only strips when at least 3 letters remain.
    """
    for suffix in ("ing", "ion", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def tokenize(text):
    """
    Lowercase, split on anything that is not a letter or digit, drop
    stopwords, and stem. "generate_access_token" -> ["generat", "access", "token"].
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [stem(w) for w in words if w not in STOPWORDS]


def split_into_sections(text):
    """
    Split a markdown document into sections. A new section starts at every
    heading line ("# ...", "## ...", "### ..."), so each section keeps its
    heading together with the text under it, e.g. "### GET /api/users" stays
    attached to "Returns a list of all users."

    Sections that are only a heading with no body are dropped.
    """
    sections = []
    current = []
    for line in text.splitlines():
        if re.match(r"^#{1,6} ", line) and current:
            sections.append("\n".join(current).strip())
            current = []
        current.append(line)
    if current:
        sections.append("\n".join(current).strip())

    return [s for s in sections if len(s.splitlines()) > 1]


class DocuBot:
    def __init__(self, docs_folder="docs", llm_client=None):
        """
        docs_folder: directory containing project documentation files
        llm_client: optional Gemini client for LLM based answers
        """
        self.docs_folder = docs_folder
        self.llm_client = llm_client

        # Load documents into memory
        self.documents = self.load_documents()  # List of (filename, text)

        # Break documents into smaller sections; these are the unit of retrieval
        self.sections = []  # List of (filename, section_text)
        for filename, text in self.documents:
            for section in split_into_sections(text):
                self.sections.append((filename, section))

        # Build a retrieval index (implemented in Phase 1)
        self.index = self.build_index(self.documents)

    # -----------------------------------------------------------
    # Document Loading
    # -----------------------------------------------------------

    def load_documents(self):
        """
        Loads all .md and .txt files inside docs_folder.
        Returns a list of tuples: (filename, text)
        """
        docs = []
        pattern = os.path.join(self.docs_folder, "*.*")
        for path in glob.glob(pattern):
            if path.endswith(".md") or path.endswith(".txt"):
                with open(path, "r", encoding="utf8") as f:
                    text = f.read()
                filename = os.path.basename(path)
                docs.append((filename, text))
        return docs

    # -----------------------------------------------------------
    # Index Construction (Phase 1)
    # -----------------------------------------------------------

    def build_index(self, documents):
        """
        Build a tiny inverted index mapping each (stemmed, lowercase) word to
        the sections it appears in.

        Because we retrieve sections rather than whole files, the index
        stores section ids (positions in self.sections) instead of filenames:
        {
            "token": [3, 4, 7, 12],
            "databas": [15, 16, 22],
        }

        `documents` is kept as a parameter to match the original signature;
        the sections were already derived from it in __init__.
        """
        index = {}
        for section_id, (_, text) in enumerate(self.sections):
            for word in set(tokenize(text)):
                index.setdefault(word, []).append(section_id)
        return index

    # -----------------------------------------------------------
    # Scoring and Retrieval (Phase 1)
    # -----------------------------------------------------------

    def score_document(self, query, text):
        """
        Relevance score for one section. Three simple parts:

        1. Coverage: number of DISTINCT meaningful query words in the text.
           Counting distinct words (not total occurrences) stops a long
           section that repeats "token" ten times from beating a short one
           that mentions every word in the question once.
        2. Heading bonus: +1 for each query word that also appears in the
           section's first line (its heading). "### GET /api/users" is a
           better match for "users" than a section that only says "username".
        3. Exact-term bonus: +2 for each code-like term in the query (it
           contains "/" or "_", like "/api/projects/<project_id>" or
           "auth_utils.py") that appears verbatim in the text.
        """
        query_words = set(tokenize(query))
        heading = text.splitlines()[0] if text else ""

        coverage = len(query_words & set(tokenize(text)))
        heading_bonus = len(query_words & set(tokenize(heading)))

        exact_bonus = 0
        for term in re.findall(r"\S*[/_]\S*", query.lower()):
            term = term.strip("?.,!'\"`")
            if term and term in text.lower():
                exact_bonus += 2

        return coverage + heading_bonus + exact_bonus

    def retrieve(self, query, top_k=3):
        """
        Select the top_k most relevant sections for the query.

        Steps:
        1. Use the index to collect candidate sections that share at least
           one word with the query (everything else would score 0).
        2. Guardrail: drop candidates that contain less than
           MIN_MATCH_FRACTION of the query's meaningful words. This uses
           plain coverage, NOT the score, so heading/exact bonuses can only
           reorder evidence, never turn weak evidence into "enough".
        3. Score the survivors with score_document.
        4. Sort by score (highest first) and keep top_k.

        Returns a list of (filename, section_text). An empty list means
        "no meaningful evidence", and callers turn that into a refusal.
        """
        query_words = set(tokenize(query))
        if not query_words:
            return []

        candidate_ids = set()
        for word in query_words:
            candidate_ids.update(self.index.get(word, []))

        scored = []
        for section_id in candidate_ids:
            filename, text = self.sections[section_id]

            coverage = len(query_words & set(tokenize(text))) / len(query_words)
            if coverage < MIN_MATCH_FRACTION:
                continue  # not enough evidence in this section

            scored.append((self.score_document(query, text), section_id))

        # Highest score first; ties keep document order (lower id first)
        scored.sort(key=lambda pair: (-pair[0], pair[1]))

        results = [self.sections[section_id] for _, section_id in scored]
        return results[:top_k]

    # -----------------------------------------------------------
    # Answering Modes
    # -----------------------------------------------------------

    def answer_retrieval_only(self, query, top_k=3):
        """
        Phase 1 retrieval only mode.
        Returns raw snippets and filenames with no LLM involved.
        """
        snippets = self.retrieve(query, top_k=top_k)

        if not snippets:
            return "I do not know based on these docs."

        formatted = []
        for filename, text in snippets:
            formatted.append(f"[{filename}]\n{text}\n")

        return "\n---\n".join(formatted)

    def answer_rag(self, query, top_k=3):
        """
        Phase 2 RAG mode.
        Uses student retrieval to select snippets, then asks Gemini
        to generate an answer using only those snippets.
        """
        if self.llm_client is None:
            raise RuntimeError(
                "RAG mode requires an LLM client. Provide a GeminiClient instance."
            )

        snippets = self.retrieve(query, top_k=top_k)

        if not snippets:
            return "I do not know based on these docs."

        return self.llm_client.answer_from_snippets(query, snippets)

    # -----------------------------------------------------------
    # Bonus Helper: concatenated docs for naive generation mode
    # -----------------------------------------------------------

    def full_corpus_text(self):
        """
        Returns all documents concatenated into a single string.
        This is used in Phase 0 for naive 'generation only' baselines.
        """
        return "\n\n".join(text for _, text in self.documents)
