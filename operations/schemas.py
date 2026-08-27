"""Public Pydantic contracts for the Repository Operations Agent.

Everything crossing a module, process or API boundary in the operations layer
is one of the models below. The design rules:

* **Operations are an enum, never free text.** An LLM picks a member of
  :class:`OperationType`; it can never hand us a shell string.
* **Findings carry provenance.** Every :class:`CodeMatch` records how it was
  detected (:class:`DetectionMethod`) and how much to trust it
  (:class:`Confidence`), so an inferred relationship is never presented as a
  confirmed one.
* **Results are uniform.** Every operation returns an
  :class:`OperationResult`, so callers (API, ADK tools, other agents) have a
  single shape to handle.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Enumerations
# --------------------------------------------------------------------------


class OperationType(str, Enum):
    """Every operation the agent is allowed to request.

    The first block matches the initially specified operation set; the second
    block covers the remaining agent responsibilities (git metadata, call
    graphs, extra diagram kinds) and the still-disabled development-validation
    entry point.
    """

    LIST_REPOSITORY_FILES = "list_repository_files"
    COUNT_FILES_AND_DIRECTORIES = "count_files_and_directories"
    DETECT_LANGUAGES = "detect_languages"
    FIND_CLASS = "find_class"
    FIND_INTERFACE = "find_interface"
    FIND_FUNCTION = "find_function"
    FIND_METHOD = "find_method"
    FIND_SYMBOL = "find_symbol"
    FIND_REFERENCES = "find_references"
    FIND_IMPLEMENTATIONS = "find_implementations"
    FIND_INHERITANCE = "find_inheritance"
    FIND_IMPORTS = "find_imports"
    READ_FILE_RANGE = "read_file_range"
    ANALYZE_OOP = "analyze_oop"
    BUILD_RELATIONSHIP_GRAPH = "build_relationship_graph"
    GENERATE_CLASS_DIAGRAM = "generate_class_diagram"
    GENERATE_INHERITANCE_DIAGRAM = "generate_inheritance_diagram"
    GENERATE_DEPENDENCY_DIAGRAM = "generate_dependency_diagram"

    # Additional operations required by the agent's responsibilities.
    FIND_CALLS = "find_calls"
    GIT_METADATA = "git_metadata"
    GENERATE_COMPONENT_DIAGRAM = "generate_component_diagram"
    GENERATE_SEQUENCE_DIAGRAM = "generate_sequence_diagram"

    # Development-validation profile only; disabled by default.
    RUN_STATIC_ANALYSIS = "run_static_analysis"


class SymbolType(str, Enum):
    """Kind of source symbol a match refers to."""

    CLASS = "class"
    ABSTRACT_CLASS = "abstract_class"
    INTERFACE = "interface"
    STRUCT = "struct"
    ENUM = "enum"
    RECORD = "record"
    TYPE_ALIAS = "type_alias"
    FUNCTION = "function"
    METHOD = "method"
    CONSTRUCTOR = "constructor"
    FIELD = "field"
    PARAMETER = "parameter"
    MODULE = "module"
    PACKAGE = "package"
    UNKNOWN = "unknown"


class RelationType(str, Enum):
    """Typed edge between two symbols."""

    INHERITS = "INHERITS"
    IMPLEMENTS = "IMPLEMENTS"
    CALLS = "CALLS"
    IMPORTS = "IMPORTS"
    CONTAINS = "CONTAINS"
    USES = "USES"
    READS = "READS"
    WRITES = "WRITES"
    PUBLISHES = "PUBLISHES"
    CONSUMES = "CONSUMES"


class Confidence(str, Enum):
    """How strongly the evidence supports a finding.

    ``HIGH``   — confirmed by a real parser (Python ``ast``) or an explicit,
                 unambiguous syntactic declaration (``extends``, ``implements``).
    ``MEDIUM`` — derived from a reliable lexical parse, or a structural match
                 (e.g. Go method-set matching) that could in principle be
                 shadowed by code we did not parse.
    ``LOW``    — candidate discovered by text search only, or a name that
                 resolved ambiguously to several symbols.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DetectionMethod(str, Enum):
    """How a finding was produced. Recorded on every match and every edge."""

    PYTHON_AST = "python_ast"
    TREE_SITTER = "tree_sitter"
    AST_GREP = "ast_grep"
    CTAGS = "ctags"
    LEXICAL_PARSE = "lexical_parse"
    TEXT_SEARCH = "text_search"
    RIPGREP = "ripgrep"
    GIT_CLI = "git_cli"
    FILESYSTEM = "filesystem"
    DERIVED = "derived"


class Visibility(str, Enum):
    """Declared (or conventional) visibility of a symbol."""

    PUBLIC = "public"
    PROTECTED = "protected"
    PRIVATE = "private"
    PACKAGE = "package"
    INTERNAL = "internal"
    UNKNOWN = "unknown"


# --------------------------------------------------------------------------
# Evidence and matches
# --------------------------------------------------------------------------


class SourceEvidence(BaseModel):
    """A concrete pointer into source code backing a finding."""

    file_path: str = Field(description="Repository-relative path.")
    line: int | None = Field(default=None, description="1-based start line.")
    end_line: int | None = Field(default=None, description="1-based end line.")
    excerpt: str = Field(default="", description="Redacted source excerpt.")
    detection_method: DetectionMethod = DetectionMethod.TEXT_SEARCH


class CodeMatch(BaseModel):
    """One structured finding: a symbol, optionally related to another symbol."""

    file_path: str = Field(description="Repository-relative path.")
    symbol: str
    symbol_type: SymbolType = SymbolType.UNKNOWN
    relationship: RelationType | None = None
    target_symbol: str | None = None
    line: int | None = None
    end_line: int | None = None
    language: str | None = None
    visibility: Visibility = Visibility.UNKNOWN
    detection_method: DetectionMethod = DetectionMethod.TEXT_SEARCH
    confidence: Confidence = Confidence.LOW
    snippet: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# Request / result
# --------------------------------------------------------------------------


class OperationRequest(BaseModel):
    """A single, fully validated operation request.

    ``arguments`` carries operation-specific knobs (limits, globs, line
    ranges). It is deliberately a plain dict: each handler validates the keys
    it understands and ignores the rest, which keeps the enum stable as
    handlers grow options.
    """

    operation: OperationType
    repository: str
    language: str | None = None
    symbol: str | None = None
    file_path: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class OperationResult(BaseModel):
    """Uniform result envelope for every operation.

    ``status`` semantics:

    * ``success`` — the operation completed and the results are complete.
    * ``partial`` — usable results, but something was skipped (limits hit, an
      optional tool missing, some files unparsable). See ``warnings``.
    * ``failed``  — no usable results. See ``errors``.
    """

    status: Literal["success", "failed", "partial"]
    operation: OperationType
    repository: str = ""
    matches: list[CodeMatch] = Field(default_factory=list)
    evidence: list[SourceEvidence] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data: dict[str, Any] = Field(
        default_factory=dict,
        description="Operation-specific payload (counts, graph, diagram text).",
    )
    truncated: bool = False
    cache_hit: bool = False
    duration_ms: int = 0


# --------------------------------------------------------------------------
# Analyzer-facing models (used by operations.languages.*)
# --------------------------------------------------------------------------


class SymbolInfo(BaseModel):
    """A symbol declared in one file, as reported by a language analyzer."""

    name: str
    symbol_type: SymbolType
    file_path: str
    line: int
    end_line: int | None = None
    language: str = ""
    package: str = ""
    owner: str | None = Field(
        default=None, description="Enclosing type for methods/fields."
    )
    visibility: Visibility = Visibility.UNKNOWN
    is_abstract: bool = False
    is_static: bool = False
    signature: str = ""
    detection_method: DetectionMethod = DetectionMethod.LEXICAL_PARSE
    confidence: Confidence = Confidence.MEDIUM
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Language-specific extras (annotations, decorators, modifiers).",
    )

    @property
    def qualified_name(self) -> str:
        """``Owner.name`` for members, plain ``name`` for top-level symbols."""
        return f"{self.owner}.{self.name}" if self.owner else self.name


class RelationshipInfo(BaseModel):
    """A relationship between a source symbol and a (possibly unresolved) target."""

    source: str
    target: str
    relation: RelationType
    file_path: str
    line: int
    end_line: int | None = None
    language: str = ""
    detection_method: DetectionMethod = DetectionMethod.LEXICAL_PARSE
    confidence: Confidence = Confidence.MEDIUM
    metadata: dict[str, Any] = Field(default_factory=dict)


class FileAnalysis(BaseModel):
    """Everything one analyzer extracted from one file."""

    file_path: str
    language: str
    package: str = ""
    symbols: list[SymbolInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
