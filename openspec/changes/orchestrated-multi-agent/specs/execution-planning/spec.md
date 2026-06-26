## ADDED Requirements

### Requirement: Plan action in ReAct loop
The ReAct loop SHALL accept `"plan"` as a valid action type. The LLM emits a JSON `ExecutionPlan` describing a DAG of tool calls with dependencies.

#### Scenario: Agent emits plan
- **WHEN** the LLM responds with `{"action": "plan", "plan": {"description": "...", "steps": [...]}}`
- **THEN** the executor validates the plan structure, topologically sorts the DAG, and begins execution

### Requirement: ExecutionPlan DAG structure
An `ExecutionPlan` SHALL contain: `description` (string), `steps` (array of objects). Each step SHALL have: `id` (string), `tool` (string), `args` (object), `depends_on` (array of step IDs, default empty).

#### Scenario: Valid plan with dependencies
- **WHEN** a plan has steps A (depends_on: []), B (depends_on: ["A"]), C (depends_on: ["A"])
- **THEN** A runs first; B and C run in parallel after A completes

### Requirement: Parallel execution of independent steps
The executor SHALL run steps with no unresolved dependencies concurrently, spawning sub-agents for each step. The parent agent blocks until all parallel steps complete.

#### Scenario: Three independent steps
- **WHEN** a plan has three steps with empty `depends_on`
- **THEN** all three are spawned as sub-agents simultaneously and the parent waits for all results

### Requirement: Sequential execution of dependent steps
Steps with `depends_on` SHALL wait until all referenced steps complete successfully before starting. If any dependency fails, the dependent step is marked failed and skipped.

#### Scenario: Dependent step waits
- **WHEN** step B depends on step A, and A takes 5 seconds
- **THEN** B does not start until A completes, and receives A's result in its context

### Requirement: Result templating
The executor SHALL support `{{step_id}}` placeholders in step `args`. Before executing a step, these placeholders are replaced with the JSON-serialized results of the referenced steps.

#### Scenario: Result passed to synthesis step
- **WHEN** step `report` has args `{"task": "Synthesize: {{cpu}} {{memory}}"}`
- **THEN** the `cpu` and `memory` step results are injected into the task string before spawning the sub-agent

### Requirement: Plan execution timeout
The executor SHALL enforce a configurable timeout per plan execution (default 300s). If the timeout is exceeded, running sub-agents are cancelled and the plan returns a failure.

#### Scenario: Plan times out
- **WHEN** a plan exceeds the configured timeout
- **THEN** all running sub-agents are cancelled and the executor returns `{"success": False, "error": "Plan execution timed out after 300s"}`

### Requirement: Plan completion feeds back into conversation
After plan execution completes (success or failure), the results SHALL be appended to the ReAct conversation history as a single user message, enabling the LLM to continue or finish.

#### Scenario: Plan results fed back
- **WHEN** a 3-step plan completes with results R1, R2, R3
- **THEN** a user message is appended: `{"role": "user", "content": "Plan execution results:\n{\"step1\": {...}, ...}"}`
