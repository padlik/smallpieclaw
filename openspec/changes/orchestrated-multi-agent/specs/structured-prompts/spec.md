## ADDED Requirements

### Requirement: Prompt sections are file-based
The system SHALL load system prompt sections from `prompts/system/*.md` files rather than a hardcoded Python string.

#### Scenario: Startup loads sections
- **WHEN** the agent starts
- **THEN** it discovers all `.md` files in `prompts/system/`, parses YAML frontmatter, validates structure, and assembles the system prompt in declared order

### Requirement: Jinja2 templating for variables
Each section SHALL support Jinja2 variable substitution using `{{variable_name}}` syntax. Variables are declared in frontmatter and provided at build time.

#### Scenario: Memory section renders dynamic content
- **WHEN** building the system prompt with `memory_text="key: value"`
- **THEN** the section containing `PERSISTENT MEMORY:\n{{memory_text}}` renders as `PERSISTENT MEMORY:\nkey: value`

### Requirement: Section ordering
Sections SHALL be assembled in ascending `order` value. The system SHALL reject duplicate `order` values.

#### Scenario: Sections ordered correctly
- **WHEN** sections have orders 1, 2, 5, 10
- **THEN** the assembled prompt contains sections in that exact sequence

### Requirement: Mode-conditional inclusion
Each section SHALL declare which creativity modes it applies to (`all`, `default`, `planner`, `explorer`, `resilient`, or a list). Sections not matching the active mode are excluded from assembly.

#### Scenario: Planner mode includes planning section
- **WHEN** creativity mode is `planner`
- **THEN** sections with `mode: planner` are included and sections with `mode: explorer` are excluded

### Requirement: Validation at load time
The prompt loader SHALL validate: all required sections present, no unresolved variables, no mode conflicts (two sections with same mode and conflicting `conflicts_with`), and all declared variables provided at build time.

#### Scenario: Missing required section
- **WHEN** the `response-format` section (required: true) is missing from `prompts/system/`
- **THEN** the agent fails to start with a clear error naming the missing section

### Requirement: Backward compatibility
The system SHALL fall back to the legacy `SYSTEM_PROMPT_TEMPLATE` if `prompts/system/` directory does not exist, logging a deprecation warning.

#### Scenario: Legacy mode fallback
- **WHEN** no `prompts/` directory exists
- **THEN** the agent uses the hardcoded template from `prompt_builder.py` and logs a warning
