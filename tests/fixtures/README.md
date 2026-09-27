# Test fixtures

## `runs-v1/`

Two run records written by dns-bench 1.0.0 during real benchmarks, used by
`tests/test_storage.py` (`V1RunFixturesTest`). They pin what an old run file looks like, so changes to
the run format or to how runs are loaded are tested against real data, not only against records that
today's code builds.

| File | What it covers |
|---|---|
| `20260925T090918Z.json` | 5 resolvers, 1 round, 2 timeouts (`ms` and `rcode` are `null`) |
| `20260925T091918Z.json` | 5 resolvers, 2 rounds, a tie between resolvers and between two servers |

They are copies of files from a real `runs/` directory, with every occurrence (values, keys and text)
of these replaced:

| Original | Replaced with |
|---|---|
| the machine's hostname | `example-host` |
| the ISP resolver's two IPs | `192.0.2.53`, `192.0.2.54` (TEST-NET-1, reserved for documentation) |
| a personal domain in the domain list | `example.com` |

Everything else, including every measurement, is unchanged. The tests check that today's analysis of
the raw results reproduces the stored summary and recommendation exactly. Don't edit these files by
hand: add a new fixture instead.
