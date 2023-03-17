import logging
import os
import numpy as np
import pandas as pd
import shutil
import argparse
import dill as pickle
from smac import BlackBoxFacade, Callback
from itertools import combinations

from utils.utils import get_surrogate_predictions
from utils.smac_utils import run_smac_optimization
from utils.functions_utils import get_functions2d, NamedFunction
from utils.model_utils import get_hyperparams, get_classifier_from_run_conf


class SurrogateModelCallback(Callback):
    def on_next_configurations_end(self, config, config_selector):
        if config._acquisition_function._eta:
            surrogate_model = config._model
            processed_configs = len(config._processed_configs)
            if processed_configs in n_samples_spacing:
                with open(
                        f"{sampling_run_dir}/surrogates/n_eval{n_samples}_samples{processed_configs}_seed{seed}.pkl",
                        "wb") as surrogate_file:
                    pickle.dump(surrogate_model, surrogate_file)


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument('--job_id')
    args = parser.parse_args()
    job_id = args.job_id

    n_samples_spacing = np.linspace(20, 200, 10, dtype=int).tolist()
    functions = get_functions2d()
    n_seeds = 5
    #models = ["MLP", "SVM", "BDT", "DT"]
    models = functions
    data_sets = ["digits", "iris"]
    use_random_samples = False
    evaluate_on_surrogate = False
    surrogate_n_samples = 400

    init_design_max_ratio = 0.25
    init_design_n_configs_per_hyperparamter = 8
    sampling_dir_name = "runs_sampling"

    run_configs = []
    for model in models:
        if isinstance(model, NamedFunction):
            run_configs.append({"model": model, "data_set_name": None})
        else:
            hyperparams = get_hyperparams(model_name=model)
            hp_comb = combinations(hyperparams, 2)
            for hp_conf in hp_comb:
                for ds in data_sets:
                    run_configs.append({"model": model, hp_conf[0]: True, hp_conf[1]: True, "data_set_name": ds})
    run_conf = run_configs[int(job_id)]
    if run_conf['data_set_name']:
        data_set_postfix = f"_{run_conf['data_set_name']}"
    else:
        data_set_postfix = ""
    model = run_conf.pop("model")
    if isinstance(model, NamedFunction):
        classifier = model
    else:
        classifier = get_classifier_from_run_conf(model_name=model, run_conf=run_conf)

    function_name = classifier.name if isinstance(classifier, NamedFunction) else model
    optimized_parameters = classifier.configspace.get_hyperparameters()
    parameter_names = [param.name for param in optimized_parameters]

    if use_random_samples:
        run_type = "rand"
        n_samples_to_eval = [max(n_samples_spacing)]
    else:
        # SMAC uses at most scenario.n_trials * max_ratio number of configurations in the initial design
        # If we run SMAC only once with n_trials = max(n_samples_spacing), we would always use the maximum number of
        # initial designs, e.g. a run with 20 samples would have the same number of initial designs as a run with 200
        # Thus, we do separate runs for each sample size as long as the number of initial designs would differ
        if evaluate_on_surrogate:
            run_type = "surr"
            n_samples_to_eval = n_samples_spacing
        else:
            run_type = "smac"
            n_samples_to_eval = [n for n in n_samples_spacing if init_design_max_ratio * n < len(
                optimized_parameters) * init_design_n_configs_per_hyperparamter]
            if max(n_samples_spacing) not in n_samples_to_eval:
                n_samples_to_eval.append(max(n_samples_spacing))

    run_name = f"{function_name.replace(' ', '_')}_{'_'.join(parameter_names)}{data_set_postfix}"

    sampling_dir = f"learning_curves/{sampling_dir_name}/{run_type}"
    sampling_run_dir = f"{sampling_dir}/{run_name}"
    if os.path.exists(sampling_run_dir):
        shutil.rmtree(sampling_run_dir)
    os.makedirs(sampling_run_dir)
    if run_type == "smac":
        os.makedirs(f"{sampling_run_dir}/surrogates")

    with open(f"{sampling_run_dir}/classifier.pkl", "wb") as classifier_file:
        pickle.dump(classifier, classifier_file)

    # setup logging
    logger = logging.getLogger(__name__)

    logger.info(f"Start {run_type} sampling for {run_name}.")

    for n_samples in n_samples_to_eval:

        # required for surrogate evaluation
        if init_design_max_ratio * n_samples < len(
                optimized_parameters) * init_design_n_configs_per_hyperparamter:
            n_eval = n_samples
        else:
            n_eval = max(n_samples_spacing)

        df_samples = pd.DataFrame()

        for i in range(n_seeds):
            seed = i * 3

            np.random.seed(seed)

            if not isinstance(classifier, NamedFunction):
                classifier.set_seed(seed)

            logger.info(f"Sample configs and train {function_name} with seed {seed}.")

            if use_random_samples:
                configurations = classifier.configspace.sample_configuration(size=n_samples)
                performances = np.array(
                    [classifier.train(config=x, seed=seed) for x in configurations]
                )
                configurations = np.array(
                    [list(i.get_dictionary().values()) for i in configurations]
                ).T
            elif evaluate_on_surrogate:
                configurations = classifier.configspace.sample_configuration(size=surrogate_n_samples)
                configurations = np.array(
                    [list(i.get_dictionary().values()) for i in configurations]
                ).T
                with open(f"learning_curves/{sampling_dir_name}/smac/{run_name}/surrogates/n_eval{n_eval}"
                          f"_samples{n_samples}_seed{seed}.pkl", "rb") as surrogate_file:
                    surrogate_model = pickle.load(surrogate_file)
                performances = np.array(get_surrogate_predictions(configurations.T, classifier, surrogate_model))
            else:
                configurations, performances, _ = run_smac_optimization(
                    configspace=classifier.configspace,
                    facade=BlackBoxFacade,
                    target_function=classifier.train,
                    function_name=function_name,
                    n_eval=n_samples,
                    run_dir=sampling_run_dir,
                    seed=seed,
                    n_configs_per_hyperparamter=init_design_n_configs_per_hyperparamter,
                    max_ratio=init_design_max_ratio,
                    callback=SurrogateModelCallback()
                )

            df = pd.DataFrame(
                data=np.concatenate((configurations.T,
                                     performances.reshape(-1, 1)), axis=1),
                columns=parameter_names + ["cost"])
            df.insert(0, "seed", seed)
            df = df.reset_index()
            df = df.rename(columns={"index": "n_samples"})
            df["n_samples"] = df["n_samples"] + 1
            df_samples = pd.concat((df_samples, df))

            df_samples.to_csv(f"{sampling_run_dir}/samples_{n_samples}.csv", index=False)
