# CarePath

CarePath helps a referral worker find a facility for a specific service near a city, district, postal code or point in India. It shows why each facility was selected, the original source text, and the uncertainty about current availability.

## Run on Databricks Free Edition

1. In Databricks Marketplace, install **Virtue Foundation Dataset (DAIS 2026)** in your workspace.
2. Download [CarePath.ipynb](CarePath.ipynb). In Databricks **Workspace**, choose **Import** and select the file.
3. Connect **Serverless** and select **Run all**. The default search is **dialysis near Patna, Bihar**. Select a different service and edit the location in the notebook widgets to search again.

The notebook contains the project code and browser assets. It uses a prepared Delta release when that release is accessible. A fresh workspace builds its own Delta tables from Marketplace under the catalog configured in the notebook. The portable build can take several minutes. If your Marketplace catalog or writable catalog has a different name, change the corresponding widget.

The final cell verifies that the index returns national service options and that a sample search contains citations and unverified availability. To deploy the browser app, follow the optional bundle step in the notebook. The readable source folders mirror the notebook’s backend and frontend, so reviewers can inspect the implementation without opening a large JSON file. Data-dependent developer utilities require the authorized challenge data or private evaluation artifacts identified in those utilities.

## What the app checks

| Step | What it does |
| --- | --- |
| Normalize | Aligns facility fields, joins postal references and reshapes district survey indicators. Retains source claims and review flags. |
| Evaluate | Looks for direct, on-site statements and checks ambiguous identities and inconsistent geography. Holds weak or conflicting records for review. |
| Rank | Uses evidence, proximity, and location consistency. Shows the weights and gives a reason for each result. |
| Cite | Shows exact source text and the original field. Links are supplied at record level. Separately dated external documentation can corroborate a service at a branch. |
| Disclose uncertainty | The shortlist does not confirm appointments, emergency readiness, or current capacity. The interface prompts the worker to contact the facility. |

The first screen supports dialysis, MRI, neonatal intensive care and C-section, with limited evidence for emergency C-section. The deployed team build has additional model-review outputs. The public notebook runs the rules and evidence pipeline without the private precomputed verifier output. It embeds the model-verifier code for review.

[Team demo app](https://carepath-integration-7474659952522059.aws.databricksapps.com/) requires workspace access. [Challenge and submission information](https://cmudsc.com/datathon-fall2026/) · [Notebook import instructions](https://docs.databricks.com/aws/en/notebooks/notebook-export-import)

Source snapshot: team integration commit `c4295cf322aacf08f51d4f3984ea4d32e6874f7c`. This repository shares the submission code, lightweight review inputs, public references and required web assets. The raw datasets, evaluation answer keys, credentials and private Git history stay out of the public repository.
