"""Read RDF files."""

from pathlib import Path
from typing import Any, Dict, List, Optional

from llama_index.readers.base import BaseReader
from llama_index.readers.schema.base import Document


class RDFReader(BaseReader):
    """RDF reader."""

    def __init__(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Initialize loader."""
        super().__init__(*args, **kwargs)

        from rdflib import Graph, URIRef
        from rdflib.namespace import RDF, RDFS

    def fetch_labels(self, uri: Any, graph: Any, lang: str):
        """Fetch all labels of a URI by language."""

        return list(
            filter(lambda x: x.language in [lang, None], graph.objects(uri, RDFS.label))
        )

    def fetch_label_in_graphs(self, uri: Any, lang: str = "en"):
        """Fetch one label of a URI by language from the local or global graph."""

        labels = self.fetch_labels(uri, self.g_local, lang)
        if len(labels) > 0:
            return labels[0].value

        labels = self.fetch_labels(uri, self.g_global, lang)
        if len(labels) > 0:
            return labels[0].value

        raise Exception(f"Label not found for: {uri}")

    def load_data(
        self, file: Path, extra_info: Optional[Dict] = None
    ) -> List[Document]:
        """Parse file."""

        lang = extra_info["lang"] if extra_info is not None else "en"

        self.g_local = Graph()
        self.g_local.parse(file)

        self.g_global = Graph()
        self.g_global.parse(str(RDF))
        self.g_global.parse(str(RDFS))

        text_list = []

        for s, p, o in self.g_local:
            if p == RDFS.label:
                continue
            triple = (
                f"<{self.fetch_label_in_graphs(s, lang=lang)}> "
                f"<{self.fetch_label_in_graphs(p, lang=lang)}> "
                f"<{self.fetch_label_in_graphs(o, lang=lang)}>"
            )
            text_list.append(triple)

        text = "\n".join(text_list)

        return [Document(text, extra_info=extra_info)]
