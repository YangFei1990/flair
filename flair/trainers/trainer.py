from pathlib import Path
from typing import List, Union

import datetime, time

from torch.optim.sgd import SGD
from torch.utils.data.dataset import ConcatDataset

import torch

import flair
import flair.nn
from flair.data import Sentence, MultiCorpus, Corpus
from flair.datasets import DataLoader
from flair.training_utils import (
    init_output_file,
    WeightExtractor,
    clear_embeddings,
    EvaluationMetric,
    log_line,
    add_file_handler,
    Result,
)
from flair.optim import *

log = logging.getLogger("flair")


class ModelTrainer:
    def __init__(
        self,
        model: flair.nn.Model,
        corpus: Corpus,
        optimizer: Optimizer = SGD,
        epoch: int = 0,
        loss: float = 10000.0,
        optimizer_state: dict = None,
        scheduler_state: dict = None,
        seed: int = 1234,
    ):
        self.model: flair.nn.Model = model
        self.corpus: Corpus = corpus
        self.optimizer: Optimizer = optimizer
        self.epoch: int = epoch
        self.loss: float = loss
        self.scheduler_state: dict = scheduler_state
        self.optimizer_state: dict = optimizer_state
        self.cuda = torch.cuda.is_available()
        self.seed = seed

    def train(
        self,
        base_path: Union[Path, str],
        evaluation_metric: EvaluationMetric = EvaluationMetric.MICRO_F1_SCORE,
        learning_rate: float = 0.1,
        mini_batch_size: int = 32,
        eval_mini_batch_size: int = None,
        max_epochs: int = 100,
        anneal_factor: float = 0.5,
        patience: int = 3,
        train_with_dev: bool = False,
        monitor_train: bool = False,
        monitor_test: bool = False,
        embeddings_in_memory: bool = True,
        checkpoint: bool = False,
        save_final_model: bool = True,
        anneal_with_restarts: bool = False,
        shuffle: bool = True,
        param_selection_mode: bool = False,
        num_workers: int = 8,
        sampler: torch.utils.data.Sampler = None,
        horovod: bool = False,
        **kwargs,
    ) -> dict:

        rank = 0
        if horovod:
            import horovod.torch as hvd
            #hvd.init()
            torch.manual_seed(self.seed)
            shuffle = False
            rank = hvd.local_rank()
            #flair.device = torch.device("cuda:{}".format(hvd.local_rank()))

        if self.cuda:
            if horovod: torch.cuda.set_device(hvd.local_rank())
            torch.cuda.manual_seed(self.seed)
            self.model.cuda()
        #exargs = {'num_workers': 1, 'pin_memory': True} if (self.cuda and horovod) else {'num_workers': num_workers}
        exargs = {'num_workers': num_workers}
        if eval_mini_batch_size is None:
            eval_mini_batch_size = mini_batch_size

        # cast string to Path
        if type(base_path) is str:
            base_path = Path(base_path)

        if rank == 0: log_handler = add_file_handler(log, base_path / "training.log")

        log_line(log)
        log.info(f'Model: "{self.model}"')
        log_line(log)
        log.info(f'Corpus: "{self.corpus}"')
        log_line(log)
        log.info("Parameters:")
        log.info(f' - learning_rate: "{learning_rate}"')
        log.info(f' - mini_batch_size: "{mini_batch_size}"')
        log.info(f' - patience: "{patience}"')
        log.info(f' - anneal_factor: "{anneal_factor}"')
        log.info(f' - max_epochs: "{max_epochs}"')
        log.info(f' - shuffle: "{shuffle}"')
        log.info(f' - train_with_dev: "{train_with_dev}"')
        log_line(log)
        log.info(f'Model training base path: "{base_path}"')
        log_line(log)
        log.info(f"Evaluation method: {evaluation_metric.name}")

        # determine what splits (train, dev, test) to evaluate and log
        log_train = True if monitor_train else False
        log_test = (
            True
            if (not param_selection_mode and self.corpus.test and monitor_test)
            else False
        )
        log_dev = True if not train_with_dev else False

        # prepare loss logging file and set up header
        if rank == 0: loss_txt = init_output_file(base_path, "loss.tsv")

        weight_extractor = WeightExtractor(base_path)

        optimizer = self.optimizer(self.model.parameters(), lr=learning_rate, **kwargs)
        if self.optimizer_state is not None:
            optimizer.load_state_dict(self.optimizer_state)

        if horovod:
            # Broadcast parameters from rank 0 to all other processes.
            hvd.broadcast_parameters(self.model.state_dict(), root_rank=0)
            # Add Horovod Distributed Optimizer
            hvd.broadcast_optimizer_state(optimizer, root_rank=0)

            optimizer = hvd.DistributedOptimizer(optimizer,
                    named_parameters=self.model.named_parameters(),
                    )



        # minimize training loss if training with dev data, else maximize dev score
        anneal_mode = "min" if train_with_dev else "max"

        if isinstance(optimizer, (AdamW, SGDW)):
            log.info(f'Scheduler is ReduceLRWDOnPlateau ')
            scheduler = ReduceLRWDOnPlateau(
                optimizer,
                factor=anneal_factor,
                patience=patience,
                mode=anneal_mode,
                verbose=True,
            )
        else:
            log.info(f'Scheduler is ReduceLROnPlateau ')
            scheduler = ReduceLROnPlateau(
                optimizer,
                factor=anneal_factor,
                patience=patience,
                mode=anneal_mode,
                verbose=True,
            )
        if self.scheduler_state is not None:
            scheduler.load_state_dict(self.scheduler_state)

        train_data = self.corpus.train

        # if training also uses dev data, include in training set
        if train_with_dev:
            train_data = ConcatDataset([self.corpus.train, self.corpus.dev])

        if sampler is not None:
            sampler = sampler(train_data)

        if horovod:
            # Partition dataset among workers using DistributedSampler
            sampler = torch.utils.data.distributed.DistributedSampler(
                train_data, num_replicas=hvd.size(), rank=hvd.rank())

        dev_score_history = []
        dev_loss_history = []
        train_loss_history = []

        batch_loader = DataLoader(
            train_data,
            batch_size=mini_batch_size,
            shuffle=shuffle,
            sampler=sampler,
            **exargs,
        )

        # At any point you can hit Ctrl + C to break out of training early.
        try:
            previous_learning_rate = learning_rate

            for epoch in range(0 + self.epoch, max_epochs + self.epoch):
                start = time.time()
                log_line(log)

                # get new learning rate
                for group in optimizer.param_groups:
                    learning_rate = group["lr"]

                # reload last best model if annealing with restarts is enabled
                if (
                    learning_rate != previous_learning_rate
                    and anneal_with_restarts
                    and (base_path / "best-model.pt").exists()
                ):
                    log.info("resetting to best model")
                    self.model.load(base_path / "best-model.pt")

                previous_learning_rate = learning_rate

                # stop training if learning rate becomes too small
                if learning_rate < 0.0001:
                    log_line(log)
                    log.info("learning rate too small - quitting training!")
                    log_line(log)
                    break



                self.model.train()
                if horovod: sampler.set_epoch(epoch)
                train_loss: float = 0

                seen_batches = 0
                total_number_of_batches = len(batch_loader)

                modulo = max(1, int(total_number_of_batches / 10))

                # process mini-batches
                for batch_no, batch in enumerate(batch_loader):

                    loss = self.model.forward_loss(batch)

                    optimizer.zero_grad()
                    loss.backward()
                    if horovod: optimizer.synchronize()
                    torch.nn.utils.clip_grad_norm_(self.model.parameters(), 5.0)
                    if horovod:
                        with optimizer.skip_synchronize():
                            optimizer.step()
                    else:
                        optimizer.step()

                    seen_batches += 1
                    train_loss += loss.item()

                    clear_embeddings(
                        batch, also_clear_word_embeddings=not embeddings_in_memory
                    )

                    if batch_no % modulo == 0:
                        log.info(
                            f"rank:{rank}: epoch {epoch + 1} - iter {batch_no}/{total_number_of_batches} - loss "
                            f"{train_loss / seen_batches:.8f}"
                        )
                        iteration = epoch * total_number_of_batches + batch_no
                        if not param_selection_mode:
                            weight_extractor.extract_weights(
                                self.model.state_dict(), iteration
                            )

                train_loss /= seen_batches
                '''
                if horovod:
                    from mpi4py import MPI
                    comm = MPI.COMM_WORLD
                    size = comm.Get_size()
                    train_loss = comm.allreduce(train_loss, op=MPI.SUM) / size
                '''
                self.model.eval()

                log_line(log)
                if rank == 0: log.info(
                    f"rank:{rank}:EPOCH {epoch + 1} done: loss {train_loss:.4f} - lr {learning_rate:.4f}"
                )
                end = time.time()
                if rank == 0: log.info(f"Took {end-start} to process one epoch")
                # anneal against train loss if training with dev, otherwise anneal against dev score
                current_score = train_loss

                # evaluate on train / dev / test split depending on training settings
                result_line: str = ""

                if log_train:
                    train_eval_result, train_loss = self.model.evaluate(
                        self.corpus.train,
                        eval_mini_batch_size,
                        embeddings_in_memory,
                        num_workers=num_workers,
                    )
                    result_line += f"\t{train_eval_result.log_line}"

                if log_dev:
                    dev_eval_result, dev_loss = self.model.evaluate(
                        self.corpus.dev,
                        eval_mini_batch_size,
                        embeddings_in_memory,
                        num_workers=num_workers,
                    )
                    result_line += f"\t{dev_loss}\t{dev_eval_result.log_line}"
                    log.info(
                        f"rank {rank}: DEV : loss {dev_loss} - score {dev_eval_result.main_score}"
                    )
                    # calculate scores using dev data if available
                    # append dev score to score history
                    dev_score_history.append(dev_eval_result.main_score)
                    dev_loss_history.append(dev_loss)

                    current_score = dev_eval_result.main_score

                if log_test:
                    test_eval_result, test_loss = self.model.evaluate(
                        self.corpus.test,
                        eval_mini_batch_size,
                        embeddings_in_memory,
                        base_path / "test.tsv",
                        num_workers=num_workers,
                    )
                    result_line += f"\t{test_loss}\t{test_eval_result.log_line}"
                    log.info(
                        f"TEST : loss {test_loss} - score {test_eval_result.main_score}"
                    )

                # determine learning rate annealing through scheduler
                scheduler.step(current_score)

                train_loss_history.append(train_loss)

                # determine bad epoch number
                try:
                    bad_epochs = scheduler.num_bad_epochs
                except:
                    bad_epochs = 0
                for group in optimizer.param_groups:
                    new_learning_rate = group["lr"]
                if new_learning_rate != previous_learning_rate:
                    bad_epochs = patience + 1

                # log bad epochs
                log.info(f"BAD EPOCHS (no improvement): {bad_epochs}")

                # output log file
                if rank == 0:
                    with open(loss_txt, "a") as f:

                        # make headers on first epoch
                        if epoch == 0:
                            f.write(
                                f"EPOCH\tTIMESTAMP\tBAD_EPOCHS\tLEARNING_RATE\tTRAIN_LOSS"
                            )

                            if log_train:
                                f.write(
                                    "\tTRAIN_"
                                    + "\tTRAIN_".join(
                                        train_eval_result.log_header.split("\t")
                                    )
                                )
                            if log_dev:
                                f.write(
                                    "\tDEV_LOSS\tDEV_"
                                    + "\tDEV_".join(dev_eval_result.log_header.split("\t"))
                                )
                            if log_test:
                                f.write(
                                    "\tTEST_LOSS\tTEST_"
                                    + "\tTEST_".join(
                                        test_eval_result.log_header.split("\t")
                                    )
                                )

                        f.write(
                            f"\n{epoch}\t{datetime.datetime.now():%H:%M:%S}\t{bad_epochs}\t{learning_rate:.4f}\t{train_loss}"
                        )
                        f.write(result_line)

                # if checkpoint is enable, save model at each epoch
                if checkpoint and not param_selection_mode and rank == 0:
                    self.model.save_checkpoint(
                        base_path / "checkpoint.pt",
                        optimizer.state_dict(),
                        scheduler.state_dict(),
                        epoch + 1,
                        train_loss,
                    )

                # if we use dev data, remember best model based on dev evaluation score
                if (
                    not train_with_dev
                    and not param_selection_mode
                    and current_score == scheduler.best
                    and rank == 0
                ):
                    self.model.save(base_path / "best-model.pt")

            # if we do not use dev data for model selection, save final model
            if save_final_model and not param_selection_mode and rank == 0:
                self.model.save(base_path / "final-model.pt")

        except KeyboardInterrupt:
            log_line(log)
            log.info("Exiting from training early.")
            if not param_selection_mode and rank == 0:
                log.info("Saving model ...")
                self.model.save(base_path / "final-model.pt")
                log.info("Done.")
        '''
        if horovod:
            from mpi4py import MPI
            comm = MPI.COMM_WORLD
            comm.barrier()
            log.info(f"rank {rank} ready to go ...")
        '''
        # test best model if test data is present
        final_score = None
        if rank == 0:
            if self.corpus.test:
                final_score = self.final_test(
                    base_path,
                    embeddings_in_memory,
                    evaluation_metric,
                    eval_mini_batch_size,
                    num_workers,
                )
                log.info(f"For rank 0: final test finished..., final score {final_score}")
            else:
                final_score = 0
                log.info("Test data not provided setting final score to 0")

        if horovod:
            from mpi4py import MPI
            comm = MPI.COMM_WORLD
            comm.bcast(final_score, root=0)

        log.info(f"final test finished..., final score {final_score}")
        if rank == 0: log.removeHandler(log_handler)
        print("ready to return..")
        return {
            "test_score": final_score,
            "dev_score_history": dev_score_history,
            "train_loss_history": train_loss_history,
            "dev_loss_history": dev_loss_history,
        }

    def final_test(
        self,
        base_path: Path,
        embeddings_in_memory: bool,
        evaluation_metric: EvaluationMetric,
        eval_mini_batch_size: int,
        num_workers: int = 8,
    ):

        log_line(log)
        log.info("Testing using best model ...")

        self.model.eval()

        if (base_path / "best-model.pt").exists():
            self.model = self.model.load(base_path / "best-model.pt")

        test_results, test_loss = self.model.evaluate(
            self.corpus.test,
            eval_mini_batch_size=eval_mini_batch_size,
            embeddings_in_memory=embeddings_in_memory,
            out_path=base_path / "test.tsv",
            num_workers=num_workers,
        )

        test_results: Result = test_results
        log.info(test_results.log_line)
        log.info(test_results.detailed_results)
        log_line(log)

        # if we are training over multiple datasets, do evaluation for each
        if type(self.corpus) is MultiCorpus:
            for subcorpus in self.corpus.corpora:
                log_line(log)
                self.model.evaluate(
                    subcorpus.test,
                    eval_mini_batch_size,
                    embeddings_in_memory,
                    base_path / f"{subcorpus.name}-test.tsv",
                )

        # get and return the final test score of best model
        final_score = test_results.main_score
        return final_score

    @classmethod
    def load_from_checkpoint(
        cls, checkpoint, corpus: Corpus, optimizer: Optimizer = SGD
    ):
        return ModelTrainer(
            checkpoint["model"],
            corpus,
            optimizer,
            epoch=checkpoint["epoch"],
            loss=checkpoint["loss"],
            optimizer_state=checkpoint["optimizer_state_dict"],
            scheduler_state=checkpoint["scheduler_state_dict"],
        )

    def find_learning_rate(
        self,
        base_path: Union[Path, str],
        file_name: str = "learning_rate.tsv",
        start_learning_rate: float = 1e-7,
        end_learning_rate: float = 10,
        iterations: int = 100,
        mini_batch_size: int = 32,
        stop_early: bool = True,
        smoothing_factor: float = 0.98,
        **kwargs,
    ) -> Path:
        best_loss = None
        moving_avg_loss = 0

        # cast string to Path
        if type(base_path) is str:
            base_path = Path(base_path)
        learning_rate_tsv = init_output_file(base_path, file_name)

        with open(learning_rate_tsv, "a") as f:
            f.write("ITERATION\tTIMESTAMP\tLEARNING_RATE\tTRAIN_LOSS\n")

        optimizer = self.optimizer(
            self.model.parameters(), lr=start_learning_rate, **kwargs
        )

        train_data = self.corpus.train

        batch_loader = DataLoader(train_data, batch_size=mini_batch_size, shuffle=True)

        scheduler = ExpAnnealLR(optimizer, end_learning_rate, iterations)

        model_state = self.model.state_dict()
        model_device = next(self.model.parameters()).device
        self.model.train()

        for itr, batch in enumerate(batch_loader):
            loss = self.model.forward_loss(batch)

            optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), 5.0)
            optimizer.step()
            scheduler.step()
            learning_rate = scheduler.get_lr()[0]

            loss_item = loss.item()
            if itr == 0:
                best_loss = loss_item
            else:
                if smoothing_factor > 0:
                    moving_avg_loss = (
                        smoothing_factor * moving_avg_loss
                        + (1 - smoothing_factor) * loss_item
                    )
                    loss_item = moving_avg_loss / (1 - smoothing_factor ** (itr + 1))
                if loss_item < best_loss:
                    best_loss = loss

            if stop_early and (loss_item > 4 * best_loss or torch.isnan(loss)):
                log_line(log)
                log.info("loss diverged - stopping early!")
                break

            if itr > iterations:
                break

            with open(learning_rate_tsv, "a") as f:
                f.write(
                    f"{itr}\t{datetime.datetime.now():%H:%M:%S}\t{learning_rate}\t{loss_item}\n"
                )

        self.model.load_state_dict(model_state)
        self.model.to(model_device)

        log_line(log)
        log.info(f"learning rate finder finished - plot {learning_rate_tsv}")
        log_line(log)

        return Path(learning_rate_tsv)
