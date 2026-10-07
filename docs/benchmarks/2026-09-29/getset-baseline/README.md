# GET/SET CPU scaling — 29 September 2026

30/30 cells complete; 30 valid. Complete sweep.

Median throughput from three 60-second runs, each at or below 5 ms p99 service latency. Highest confirmed settings in a closed-loop concurrency sweep; lower bounds, not universal maximum capacities.

1 million keys, 256-byte values, pipeline 1, persistence off. CPU quotas on one c6gn.8xlarge server; 8 GiB per engine. Separate c7g.16xlarge client. These are not exclusive physical-core allocations.

## GET

| CPU quota | Vex ops/s | Redis ops/s | Dragonfly ops/s |
|---:|---:|---:|---:|
| 1 | 140,800 | 129,494 | 107,887 |
| 2 | 255,724 | 133,265 | 179,287 |
| 4 | 505,488 | 381,035 | 326,982 |
| 8 | 999,653 | 770,553 | 599,349 |
| 16 | 1,733,958 | 875,673 | 1,052,455 |

## SET

| CPU quota | Vex ops/s | Redis ops/s | Dragonfly ops/s |
|---:|---:|---:|---:|
| 1 | 140,156 | 117,705 | 101,352 |
| 2 | 256,884 | 127,516 | 174,468 |
| 4 | 509,808 | 359,680 | 309,780 |
| 8 | 1,007,557 | 731,959 | 570,702 |
| 16 | 894,516 | 760,494 | 950,037 |

## Variation and limits

| CPUs | Workload | Engine | Min–max ops/s | Median / max p99 ms | Connections | Search classification |
|---:|---|---|---:|---:|---:|---|
| 1 | GET | vex | 140,304–140,805 | 0.295 / 0.295 | 32 | invalid search trial; lower bound |
| 1 | GET | redis | 129,440–129,601 | 0.359 / 0.367 | 32 | SLO boundary observed |
| 1 | GET | dragonfly | 106,656–108,502 | 0.343 / 0.343 | 32 | SLO boundary observed |
| 1 | SET | vex | 138,561–140,328 | 0.295 / 0.303 | 32 | invalid search trial; lower bound |
| 1 | SET | redis | 113,323–124,915 | 0.407 / 0.423 | 32 | SLO boundary observed |
| 1 | SET | dragonfly | 82,462–101,657 | 0.367 / 0.439 | 32 | SLO boundary observed |
| 2 | GET | vex | 254,287–256,426 | 2.223 / 2.287 | 512 | SLO boundary observed |
| 2 | GET | redis | 132,856–133,503 | 1.623 / 1.631 | 128 | SLO boundary observed |
| 2 | GET | dragonfly | 176,048–181,681 | 0.287 / 0.295 | 32 | SLO boundary observed |
| 2 | SET | vex | 256,099–257,508 | 2.159 / 2.271 | 512 | invalid search trial; lower bound |
| 2 | SET | redis | 125,627–127,848 | 1.735 / 1.743 | 128 | SLO boundary observed |
| 2 | SET | dragonfly | 172,015–181,502 | 4.351 / 4.511 | 512 | SLO boundary observed |
| 4 | GET | vex | 503,301–508,815 | 1.343 / 1.431 | 512 | SLO boundary observed |
| 4 | GET | redis | 368,363–382,674 | 2.367 / 2.655 | 512 | SLO boundary observed |
| 4 | GET | dragonfly | 326,064–330,618 | 0.647 / 0.647 | 128 | SLO boundary observed |
| 4 | SET | vex | 509,039–513,363 | 1.191 / 1.319 | 512 | SLO boundary observed |
| 4 | SET | redis | 358,644–365,601 | 2.671 / 2.767 | 512 | SLO boundary observed |
| 4 | SET | dragonfly | 307,294–309,902 | 0.687 / 0.743 | 128 | SLO boundary observed |
| 8 | GET | vex | 996,508–1,002,431 | 0.815 / 1.031 | 512 | SLO boundary observed |
| 8 | GET | redis | 759,894–781,176 | 1.215 / 1.383 | 512 | SLO boundary observed |
| 8 | GET | dragonfly | 576,190–612,672 | 1.607 / 1.727 | 512 | SLO boundary observed |
| 8 | SET | vex | 995,911–1,015,203 | 0.983 / 1.071 | 512 | SLO boundary observed |
| 8 | SET | redis | 717,378–778,591 | 1.535 / 1.543 | 512 | SLO boundary observed |
| 8 | SET | dragonfly | 558,002–583,288 | 1.455 / 1.535 | 512 | SLO boundary observed |
| 16 | GET | vex | 1,725,508–1,736,255 | 1.759 / 1.775 | 512 | SLO boundary observed |
| 16 | GET | redis | 823,398–878,068 | 2.847 / 3.007 | 2048 | SLO boundary observed |
| 16 | GET | dragonfly | 1,049,859–1,056,338 | 1.679 / 2.079 | 512 | SLO boundary observed |
| 16 | SET | vex | 880,734–898,501 | 0.303 / 0.319 | 128 | client-limited lower bound |
| 16 | SET | redis | 717,913–910,901 | 3.199 / 3.391 | 2048 | SLO boundary observed |
| 16 | SET | dragonfly | 943,143–958,274 | 3.647 / 3.727 | 2048 | SLO boundary observed |

A rejected search trial is excluded from capacity calculations. The frozen Vex binary predates the dynamic descriptor-table fix. Connection-accept bursts can also reject trials below the old descriptor ceiling. Raw trials, errors, hashes, topology, CPU and ENA snapshots remain in the run directory.
