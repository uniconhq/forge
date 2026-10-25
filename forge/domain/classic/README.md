# unicon/classic@v3

One run of an entrant's program on one input. `workflow.yaml` compiles the
entrant's code with `unicon/compile@v3`, runs the program with
`unicon/exec@v1` on the run's `input` under the task's limits, and runs
`diff/diff.py`, compiled once at the save, with exec, given
`diff/program.yaml` as its manifest and the program's output and the run's
`answer` as named files. A task repeats it over its tests with `repeat`.

The entrant gives a folder of code and its language, one of `c`, `cpp`,
`java` and `python`; the task may narrow the languages. The task gives
`time_limit`, in seconds, and `memory_limit`, in megabytes, and each run's
`input` and `answer`.

Code that does not compile ends every run `compile_error`. Otherwise a
run's verdict is the program's, when it went over a limit or crashed, or
else the diff script's: `accepted` when the output's words are the
answer's, split on spaces, tabs, carriage returns and line feeds, as
testlib's `wcmp` reads them, and `wrong_answer` when they are not.

The diff script runs with a time limit of 3 s and 256 MB of memory, its
own and not the task's, enough to read two files of output. The planner
counts each step's worst case against a pipeline's 25 minutes, and
exec's is twice its time limit and 3 s more, so each run's compare
counts 9 s beside the solution's.

Each run reports `score`, 1 when accepted and 0 when not; `time_ms`, "Run
time" in ms, the program's CPU time; and `memory_kb`, "Memory used" in KB,
its peak memory. The compiler's output is `log`, "Compile log", so code
that does not compile says why.
