## ADDED Requirements

### Requirement: Agent sends Telegram notification at each step milestone

The agent SHALL send a non-blocking Telegram notification whenever `state.step` is a positive multiple of `step_notify_interval` and `step_notify_interval > 0`. The notification is fire-and-forget: it MUST NOT block the react loop thread or delay the next step. The agent continues execution immediately after dispatching the notification.

#### Scenario: Notification sent at the first milestone step
- **GIVEN** `step_notify_interval = 30` is configured
- **AND** the agent is running as the main agent (milestone_notify_fn is wired)
- **WHEN** the agent completes step 30
- **THEN** a Telegram notification is sent indicating the agent is at step 30 and still running
- **AND** the agent proceeds to step 31 without waiting for the notification to be delivered

#### Scenario: Notification sent at subsequent milestone steps
- **GIVEN** `step_notify_interval = 30`
- **AND** the agent already completed step 30 and sent a notification
- **WHEN** the agent completes step 60
- **THEN** a second Telegram notification is sent for step 60
- **AND** the pattern continues for steps 90, 120, etc.

#### Scenario: No notification at non-milestone steps
- **GIVEN** `step_notify_interval = 30`
- **WHEN** the agent completes step 15 (not a multiple of 30)
- **THEN** no Telegram notification is sent

#### Scenario: No notification at step 0
- **GIVEN** `step_notify_interval = 30`
- **WHEN** the agent initializes (step = 0)
- **THEN** no Telegram notification is sent

### Requirement: step_notify_interval = 0 disables milestone notifications

When `step_notify_interval` is set to `0`, the agent SHALL NOT send any milestone notifications regardless of how many steps complete.

#### Scenario: Zero interval disables all notifications
- **GIVEN** `step_notify_interval = 0`
- **AND** the agent completes 100 steps
- **THEN** no milestone Telegram notifications are sent at any step

### Requirement: Milestone notifications apply only to the main agent

Scheduled sub-agents and plan-step sub-agents SHALL NOT send milestone notifications. The milestone notification callback is only wired for the main (chat) agent.

#### Scenario: Scheduled job emits no milestone notifications
- **GIVEN** a scheduled job is running with `step_notify_interval = 30`
- **WHEN** the job completes step 30
- **THEN** no milestone Telegram notification is sent by the job
- **AND** the job continues running silently

#### Scenario: Plan-step sub-agent emits no milestone notifications
- **GIVEN** an execution-plan sub-agent is running
- **WHEN** the sub-agent completes step 30
- **THEN** no milestone Telegram notification is sent

### Requirement: Milestone notification is non-blocking

The `milestone_notify_fn` call MUST NOT block the react-loop thread. Dispatching the Telegram send to the bot's asyncio event loop via a fire-and-forget mechanism (e.g., `run_coroutine_threadsafe` without awaiting the result) is the required implementation approach.

#### Scenario: Agent continues immediately after milestone notification dispatch
- **GIVEN** the agent reaches a milestone step
- **AND** the Telegram network is slow or unavailable
- **WHEN** the milestone notification is dispatched
- **THEN** the react loop proceeds to the next step without waiting for Telegram delivery confirmation
