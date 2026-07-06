flowchart TD
    A["Start: goal + task<br/>(FastAPI blog app)"] --> B{"attempt loop<br/>1 to max_attempts = 5"}

    B --> C["EXECUTE<br/>chat_v2(EXECUTOR_SYSTEM, user_msg, TOOL_SCHEMAS)"]

    subgraph TOOLLOOP["chat_v2 tool loop, max 15 rounds"]
        C1["POST /api/chat"] --> C2{"tool_calls in reply?"}
        C2 -->|"no"| C3["final text returned"]
        C2 -->|"yes"| C4["execute_tool_call<br/>- parses JSON-string args<br/>- str() coerces result<br/>- ALL exceptions become [ERROR] strings<br/>fed back so the model self-corrects"]
        C4 --> C5["write_file<br/>returns 'WROTE n chars' (was None)"]
        C4 --> C6["run_python<br/>stdout / stderr / exit code<br/>TimeoutExpired caught, not raised"]
        C5 --> C7["append role: tool + tool_name<br/>(Ollama needs it to match the call)"]
        C6 --> C7
        C7 --> C1
        C2 -->|"15 rounds exhausted"| C8["one forced final call<br/>fresh payload, no tools key at all"]
    end

    C --> D["REVIEW: review() reuses chat_v2<br/>REVIEWER_SYSTEM as system, goal + output as user<br/>no tools, think = False"]
    D --> E{"verdict, prefix match<br/>so 'NO, because...' still counts"}

    E -->|"YES\b"| F["final_output.txt<br/>attempt_history.json<br/>DONE"]
    E -->|"NO\b"| G["save attempt_N_failed.txt<br/>user_msg = task + RETRY_NOTE<br/>previous output stapled in so the<br/>next run FIXES instead of restarting blind"]
    G --> B
    E -->|"neither"| H["attempt_N_needs_review.txt<br/>+ attempt_history.json<br/>stop, hand to a human"]

    B -->|"5 attempts, no YES"| I["warning +<br/>attempt_history.json"]

    J["attempts[] list records every<br/>output + verdict, nothing overwritten"] -.-> B

    style F fill:#2c5a2c,color:#fff
    style G fill:#5a4a2c,color:#fff
    style H fill:#444,color:#fff