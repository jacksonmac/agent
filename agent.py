
import requests
import re
import functools
import time
import os

from typing import Optional


#this should be taken out
#TODO CHANGE THIS
HERE = os.path.dirname(__file__) #going to do this a diffent way later

URL = "http://192.168.1.134:11434"

# ─── Prompts ──────────────────────────────────────────────────
 
# Standing instructions for the planner (goes in the system message).
PLANNER_SYSTEM = """You are a planning assistant. Produce a concrete, actionable plan.  
Given the user's goal and situation, return:
1. A plan broken into sequenced phases/milestones — what comes before what, and why.
2. For each phase: the concrete actions, and what "done" looks like before moving on.
3. The critical path — the few actions that actually drive the outcome vs. the optional ones.
4. The riskiest assumptions, and the earliest cheap way to test each.
 
Where details are missing, make a reasonable assumption and label it. Do not ask questions back."""
 
# Standing instructions for the executor.
EXECUTOR_SYSTEM = """You are executing a plan to achieve a goal. Do the work — produce real,
complete, usable output (the actual code/draft/artifact). Do not re-plan or describe what you
would do.
 
Where a step needs a fact or action you don't have, make the most reasonable assumption, label
it, and keep going. Check your output against the goal before finishing.
 
End with a short report:
- DONE: what you produced.
- STATE NOW: what is now true that wasn't before.
- ASSUMPTIONS: anything you assumed.
- NEXT: the single next action."""

review_prob = """
Did we meet the goal {goal_var} based on everything you have seen so far in:
EXECUTING output: {output_var}
Situation: {situation_var}

YOUR OUTPUT SHOULD BE "YES" OR "NO" not one word more, without any spaces or spical characters."""
 
worker_probt = """
We haven't achieved our goal: {goal_var}
Take all the data from the past agents {situation_var}
look at the plan: {plan_var}
Current work on the project is {exe1_var}

output the solution to the goal as best you can, only give output that is part of the solution, 
"""

total_time = {}

# ─── Time function ──────────────────────────────────────────────────

#TODO
#fuctnion to get current, time and run the code, and find out how much time has gone by
#STUDY THIS CODE
def timed(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        elapsed = time.perf_counter() - start
        print(f"[{func.__name__}] took {elapsed:.2f}s")
        total_time[func.__name__] = elapsed
        return result
    return wrapper


@timed
def chat(model, system, user, think=True) -> list[str]:
    """One system + one user turn. Returns the assistant's content."""
    resp = requests.post(
        URL + "/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "think": think,
            "stream": False,
        },
    )
    resp.raise_for_status()
    msg = resp.json()["message"]
    if msg.get("thinking"):
        print("Thinking:\n", msg["thinking"], "\n")
    print("Answer:\n", msg["content"], "\n")
    return msg["content"]

# ─── CORE CHAT WITH TOOL LOOP ───────────────────────────────────────
 
def execute_tool_call(name: str, arguments: dict) -> str:
    """Look up a tool by name and execute it with the given arguments."""
    func = tools.get(name)
    if not func:
        return f"[ERROR] Unknown tool: {name}"
    try:
        return func(**arguments)
    except TypeError as e:
        return f"[ERROR] Bad arguments for {name}: {e}"
#TODO
#Major problems
@timed
def chat_v2(model: str, system: str, user: str, tools: Optional[list], 
         think: bool = True, max_tool_rounds: int = 15) -> str:
    #OLD IDEA
    """One system + one user turn. Returns the assistant's content."""

    """one system + one user turn, with an optional tool-calling loop
    
    If tools are given , then model can call tools. each time it does we will 
    execute them and feed results back until the models give a final 
    text response or hit 15
    """
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
 
    for round_num in range(max_tool_rounds):
        # Build the request payload
        payload = {
            "model": model,
            "messages": messages,
            "think": think,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
 
        resp = requests.post(URL + "/api/chat", json=payload)
        resp.raise_for_status()
        msg = resp.json()["message"]
 
        # Print thinking if present
        if msg.get("thinking"):
            print(f"  [thinking round {round_num}]:\n", msg["thinking"][:500], "\n")
 
        # Check if the model wants to call tools
        tool_calls = msg.get("tool_calls")
 
        if not tool_calls:
            # No tool calls — this is the final response
            print("Answer:\n", msg["content"], "\n")
            return msg["content"]
 
        # The model wants to call tools — process each one
        # First, add the assistant's message (with tool_calls) to history
        messages.append(msg)
 
        for tc in tool_calls:
            func_info = tc["function"]
            tool_name = func_info["name"]
            tool_args = func_info.get("arguments", {})
 
            print(f"  [tool call] {tool_name}({json.dumps(tool_args)[:200]})")
            result = execute_tool_call(tool_name, tool_args)
            print(f"  [tool result] {result[:300]}")
 
            # Add the tool response to the conversation
            messages.append({
                "role": "tool",
                "content": result,
            })
 
        # Loop continues — the model will see the tool results and either
        # call more tools or give a final text response.
 
    # If we exhaust all rounds, return whatever we have
    print("[WARNING] Hit max tool rounds, forcing final response")
    # One last call without tools to force a text summary
    payload["tools"] = []
    resp = requests.post(URL + "/api/chat", json=payload)
    resp.raise_for_status()
    msg = resp.json()["message"]
    print("Answer (forced):\n", msg["content"], "\n")
    return msg["content"]

def models() -> list[str]:
    url_models = URL + "/api/tags"
    print(f"sending request to url: {url_models}")
    
    request = requests.get(url_models)
    names = []
    for m in request.json()["models"]:
        names.append(m["name"])
        #print(f"m is {m}")
    
    return names

def write_text_file(text: str, name: str):
    path = os.path.join(HERE, name)
    with open(path, "w") as f:
        f.write(text)
    print("WROTE:", path)

def write_report(sections: dict, name: str):
    path = os.path.join(HERE, name)
    parts = []
    for key, value in sections.items():
        header = key.replace("_", " ").upper()
        parts.append(f"{'='*60}\n{header}\n{'='*60}\n{value}\n")
    with open(path, "w") as f:
        f.write("\n".join(parts))
        print("WROTE:", path)

# ─── TOOL REGISTRY ──────────────────────────────────────────────────
tools = {
    #NONE RIGHT NOW
    "write_file": write_text_file,
}

#TODO WORK ON STRUCTURE HOW TO IMPLEMENT TOOLS AND CHANGES WITH NEW CHAT METHOD
#main loop
# ─── Main loop ───────────────────────────────────────
@timed 
def main():
    #TODO
    #model picking should be dyamic
    #model = "qwen3:14b"
    #model = "qwen3.5:9b"
    model = "qwen3.6:27b"
 
    #TODO
    #THIS NEEDS TO BE DYNAMIC, based on user input, add down the line
    # Real, filled-in inputs — NOT a blank template.
    goal = "A basic, fast, multi-user blog web app with user login."
    situation = (
        "- Starting point: empty project; i need you to write and output the code.\n"
        "- Deadline: no hard deadline.\n"
        "- Resources: a few hours; comfortable with Python.\n"
        "- Constraints: keep the stack simple; run locally first."
    )

    #"*" this iterates over the list 
    print("Models:", *models(), sep="\n  ")

    #making plan
    print("\n=== PLANNING ===")
    plan = chat(
        model,
        PLANNER_SYSTEM,
        f"GOAL:\n{goal}\n\nMY SITUATION:\n{situation}",
    )

    #first run
    print("\n=== EXECUTING (Phase 1) ===")
    exe1 = chat(
        model,
        EXECUTOR_SYSTEM,
        f"GOAL:\n{goal}\n\n"
        f"THE PLAN:\n{plan}\n\n"
        f"CONTEXT AND DATA:\n{situation}\n\n"
        f"CURRENT FOCUS: Phase 1\n\n"
        "Execute Phase 1 now and produce the actual deliverable.",
    )
    print("\n===Done EXECUTING (Phase 1) ===")

    print(f"what did we get as output {exe1}")

    #How did the ouput god
    print("\n===How Did We do check: (Phase 1) ===")
    message = review_prob.format(
        goal_var = goal,
        output_var = exe1, 
        situation_var = situation
    )
    #asking the model did we complet the goal
    did_we_do_it = chat(
        model,
        message,
        exe1
    )
    print(f"how did it go output: {did_we_do_it}")

    #regex for yes and no 
    yes_pattern = re.compile(r'^(yes|y|yeah|yep|yup|sure|ok|okay)$', re.IGNORECASE)
    no_pattern = re.compile(r'^(no|n|nah|nope|nay)$', re.IGNORECASE)

    #make the list a string
    string_list = "".join(did_we_do_it)
    counter = 0 #TODO REMOVE THIS

    if yes_pattern.match(string_list):
        #the model returned yes, meaning that you did completed the goal
        print(F"WE DID IT {model}, look at my work")
        print("Writing file")
        work_file = {
            "Plan": plan,
            "WORK": exe1,
                }
        write_text_file(str(work_file), "output_file.txt")

    elif no_pattern.match(string_list):
        #the model returned no, meaning we didnt complet the goal
        goal_bool = False
        last_step = None
        print("got into the no if, meaning your goal isnt done")
        while(goal_bool !=True):

            message_loop = worker_probt.format(
                goal_var = goal,
                situation_var = situation,
                plan_var = plan,
                exe1_var = exe1
            )
            next_step = chat(
                model,
                message_loop,
                exe1
            )
            #asking the model did we complet the goal
            did_we_do_it = chat(
                model,
                message,
                #exe1
                next_step #this used to be "exe1" this was a problem
            )

            if counter == 20:
                last_step = next_step #save for review doc
                break

            #TODO, NEED TO SEE IF THIS RIGTH
            exe1 = next_step  #THIS MIGHT BE A PROBLE

            #TODO
            #REMOVE THIS
            #REMOVE PRINT LINES LATER, FOR TEST
            print(f"loop data from past step {message_loop}")
            print(f"IN LOOP, DID WE DO IT {did_we_do_it}")
            #make the list a string
            string_list = "".join(did_we_do_it)
            if yes_pattern.match(string_list):
                #the model returned yes, meaning that you did completed the goal
                goal_bool = True
                #TODO, I SHOULDNT HAVE TO DO THIS
                review_doc = {
                    "goal": goal,
                    "last_step": exe1,
                    "final_output": message_loop,
                    #"final_output": last_step,
                    }
        
                #write_text_file(str(review_doc), "output_file.txt")
                print("NEW REPORT METHOD")
                write_report(review_doc, "output_file.txt")
            #TODO
            #remove the print line
            counter = counter + 1 #TODO REMOVE THIS
            print(f"IN LOOP, DOING ANOTHER RUN COUNT {counter}")

        review_doc = {
            "goal": goal,
            "last_step": exe1,
            "final_output": last_step,
                      }
        
        write_text_file(str(review_doc), "output_file.txt")
    else:
        #the model returned something that didnt match any of the regexs for yes or no
        #dont know what we should do in this case
        print("didnt hit yer or no, going to need to review output")
        #TODO AN IDEA IS MANY RETRY WITH A DIFFERNT MODEL AT THIS POINT
        print(F"OUPUT:{exe1}")
        review_doc = {
            "goal": goal,
            "last_step": exe1,
            "yes_or_no": did_we_do_it,
                      }
        write_text_file(str(review_doc), "output_file.txt")



    
#TODO
#IMPLENT THIS FUNCTION
#will give a str, and run the string, and return the output
#the string should, should be in python format that can be run
def python_tool():
    pass
 
 
if __name__ == "__main__":
    main()
    #time loop
    print("about of time everything took")
    for exe, total_times in total_time:
        print(f"{exe}:{total_time:.2f}s")