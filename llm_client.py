"""
Gemini client wrapper used by DocuBot.

Handles:
- Configuring the Gemini client from the GEMINI_API_KEY environment variable
- Naive "generation only" answers over the full docs corpus (Phase 0)
- RAG style answers that use only retrieved snippets (Phase 2)

Experiment with:
- Prompt wording
- Refusal conditions
- How strictly the model is instructed to use only the provided context
"""

import os
import time
from google import genai

# Central place to update the model name if needed.
# You can swap this for a different Gemini model in the future.
# Note: "gemini-flash-lite-latest" and "gemini-2.5-flash-lite" both returned
# 404 NOT_FOUND for this API key; the API recommended gemini-3.5-flash-lite.
GEMINI_MODEL_NAME = "gemini-3.5-flash-lite"


class GeminiClient:
    """
    Simple wrapper around the Gemini model.

    Usage:
        client = GeminiClient()
        answer = client.naive_answer_over_full_docs(query, all_text)
        # or
        answer = client.answer_from_snippets(query, snippets)
    """

    def __init__(self):
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "Missing GEMINI_API_KEY environment variable. "
                "Set it in your shell or .env file to enable LLM features."
            )

        self.client = genai.Client(api_key=api_key)

    def _generate(self, prompt, attempts=4):
        """
        Calls Gemini, retrying a few times on temporary overload errors
        (503 UNAVAILABLE / 429 RESOURCE_EXHAUSTED). Other errors are raised
        immediately so the callers' error messages still show them.
        """
        for attempt in range(attempts):
            try:
                response = self.client.models.generate_content(
                    model=GEMINI_MODEL_NAME,
                    contents=prompt
                )
                return (response.text or "").strip()
            except Exception as e:
                temporary = "503" in str(e) or "429" in str(e)
                if not temporary or attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)  # 1s, 2s, 4s

    # -----------------------------------------------------------
    # Phase 0: naive generation over full docs
    # -----------------------------------------------------------

    def naive_answer_over_full_docs(self, query, all_text):
        # We ignore all_text and send a generic prompt instead
        prompt = f"""
    You are a documentation assistant. 
    Answer this developer question: {query}
    """
        try:
            return self._generate(prompt)
        except Exception as e:
            return f"Unable to generate an answer. ({type(e).__name__}: {e})"

    # -----------------------------------------------------------
    # Phase 2: RAG style generation over retrieved snippets
    # -----------------------------------------------------------

    def answer_from_snippets(self, query, snippets):
        """
        Phase 2:
        Generate an answer using only the retrieved snippets.

        snippets: list of (filename, text) tuples selected by DocuBot.retrieve

        The prompt:
        - Shows each snippet with its filename
        - Instructs the model to rely only on these snippets
        - Requires an explicit "I do not know" refusal when needed
        """

        if not snippets:
            return "I do not know based on the docs I have."

        context_blocks = []
        for filename, text in snippets:
            block = f"File: {filename}\n{text}\n"
            context_blocks.append(block)

        context = "\n\n".join(context_blocks)

        prompt = f"""
You are a cautious documentation assistant helping developers understand a codebase.

You will receive:
- A developer question
- A small set of snippets from project files

Your job:
- Answer the question using only the information in the snippets.
- If the snippets do not provide enough evidence, refuse to guess.

Snippets:
{context}

Developer question:
{query}

Rules:
- Use only the information in the snippets. Do not invent new functions,
  endpoints, or configuration values.
- If the snippets are not enough to answer confidently, reply exactly:
  "I do not know based on the docs I have."
- When you do answer, briefly mention which files you relied on.
"""

        try:
            return self._generate(prompt)
        except Exception as e:
            return f"API error — could not generate answer. ({type(e).__name__}: {e})"
