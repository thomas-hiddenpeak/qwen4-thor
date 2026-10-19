# Real-text MoE observations

| Comparison | Position | Hidden changed | ID slots changed | Logits max abs | Common weights max abs | Output max abs |
|---|---:|---:|---:|---:|---:|---:|
| end_to_end-row0 | 72 | 0 | 0 | 0 | 0 | 0 |
| selection_intervention-row0 | 72 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_normal-row0 | 72 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_fixed_ids-row0 | 72 | 0 | 0 | 0 | 0 | 0 |
| end_to_end-row1 | 73 | 0 | 0 | 0 | 0 | 0 |
| selection_intervention-row1 | 73 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_normal-row1 | 73 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_fixed_ids-row1 | 73 | 0 | 0 | 0 | 0 | 0 |
| end_to_end-row2 | 74 | 2368 | 2 | 0.0625 | 0.00244176388 | 0.00329589844 |
| selection_intervention-row2 | 74 | 0 | 2 | 0 | 0 | 0 |
| same_input_shape_normal-row2 | 74 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_fixed_ids-row2 | 74 | 0 | 0 | 0 | 0 | 0 |
| end_to_end-row3 | 75 | 2310 | 0 | 0.03125 | 0.00349861383 | 0.00347900391 |
| selection_intervention-row3 | 75 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_normal-row3 | 75 | 0 | 0 | 0 | 0 | 0 |
| same_input_shape_fixed_ids-row3 | 75 | 0 | 0 | 0 | 0 | 0 |

One fixed text, layer 2, four forced tokens. No full-model quality or performance claim.
Different boundary hidden includes upstream effects. Fixed IDs retain current logits and weights;
remaining output differences cannot be assigned solely to routed expert arithmetic.
