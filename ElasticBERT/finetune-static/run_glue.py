import argparse
import glob
import json
import logging
import os
import random
import time

from arguments import get_args

import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange
import fitlog

import transformers
from transformers import BertTokenizer as ElasticBertTokenizer
from transformers import AdamW, get_linear_schedule_with_warmup
from transformers import glue_output_modes as output_modes
from transformers import glue_processors as processors
from transformers.trainer_utils import is_main_process

from models.configuration_elasticbert import ElasticBertConfig
from models.modeling_elasticbert import ElasticBertForSequenceClassification

from evaluations import evaluate_glue
from inferences import inference_glue
from load_data import load_and_cache_examples_glue

logger = logging.getLogger(__name__)

def get_metric_key(task_name):
    if task_name == "cola":
        return "mcc"
    elif task_name == "sst-2":
        return "acc"
    elif task_name == "mrpc":
        return "acc_and_f1"
    elif task_name == "sts-b":
        return "corr"
    elif task_name == "qqp":
        return "acc_and_f1"
    elif task_name == "mnli":
        return "mnli/acc"
    elif task_name == "mnli-mm":
        return "mnli-mm/acc"
    elif task_name == "qnli":
        return "acc"
    elif task_name == "rte":
        return "acc"
    elif task_name == "wnli":
        return "acc"
    elif task_name == "hans":
        return "acc"
    else:
        raise KeyError(task_name)

        
def set_seed(args):
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def train(args, train_dataset, model, tokenizer):
    if args.debug:
        fitlog.debug()
    if args.local_rank in [-1, 0]:
        fitlog.set_log_dir(args.log_dir)
        fitlog.add_hyper(args)

    if not os.path.exists(args.output_dir) and args.local_rank in [-1, 0]:
        os.makedirs(args.output_dir) 

    args.train_batch_size = args.per_gpu_train_batch_size * max(1, args.n_gpu)
    train_sampler = RandomSampler(train_dataset) if args.local_rank == -1 else DistributedSampler(train_dataset)
    train_dataloader = DataLoader(train_dataset, sampler=train_sampler, batch_size=args.train_batch_size)    

    if args.max_steps > 0:
        t_total = args.max_steps
        args.num_train_epochs = args.max_steps // (len(train_dataloader) // args.gradient_accumulation_steps) + 1
    else:
        t_total = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs


    # Prepare optimizer and schedule (linear warmup and decay)
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
            "weight_decay": args.weight_decay,
        },
        {"params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], "weight_decay": 0.0},
    ]  
    

    if args.warmup_steps > 0:
        num_warmup_steps = args.warmup_steps
    else:
        num_warmup_steps = args.warmup_rate * t_total

    optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
    scheduler = get_linear_schedule_with_warmup(
        optimizer, num_warmup_steps=num_warmup_steps, num_training_steps=t_total
    )

    # Check if saved optimizer or scheduler states exist
    if os.path.isfile(os.path.join(args.model_name_or_path, "optimizer.pt")) and os.path.isfile(
            os.path.join(args.model_name_or_path, "scheduler.pt")
    ):
        # Load in optimizer and scheduler states
        optimizer.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "optimizer.pt")))
        scheduler.load_state_dict(torch.load(os.path.join(args.model_name_or_path, "scheduler.pt")))

    if args.fp16:
        try:
            from apex import amp
        except ImportError:
            raise ImportError("Please install apex from https://www.github.com/nvidia/apex to use fp16 training.")
        model, optimizer = amp.initialize(model, optimizer, opt_level=args.fp16_opt_level) 

    # multi-gpu training (should be after apex fp16 initialization)
    if args.n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Distributed training (should be after apex fp16 initialization)
    if args.local_rank != -1:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
            find_unused_parameters=True,
        )

    # Train!
    logger.info("***** Running training *****")
    logger.info("  Num examples = %d", len(train_dataset))
    logger.info("  Num Epochs = %d", args.num_train_epochs)
    logger.info("  Instantaneous batch size per GPU = %d", args.per_gpu_train_batch_size)
    logger.info(
        "  Total train batch size (w. parallel, distributed & accumulation) = %d",
        args.train_batch_size
        * args.gradient_accumulation_steps
        * (torch.distributed.get_world_size() if args.local_rank != -1 else 1),
    )
    logger.info("  Gradient Accumulation steps = %d", args.gradient_accumulation_steps)
    logger.info("  Total optimization steps = %d", t_total)


    global_step = 0
    epochs_trained = 0
    steps_trained_in_current_epoch = 0


    best_all_metric = {}
    keep_best_step = 0
    tr_loss, logging_loss, best = 0.0, 0.0, 0.0
    model.zero_grad()
    train_iterator = trange(
        epochs_trained,
        int(args.num_train_epochs),
        desc="Epoch",
        disable=args.local_rank not in [-1, 0],
    )

    set_seed(args)  # Added here for reproductibility
    metric_key = get_metric_key(args.task_name)
    if args.task_name == 'mnli':
        metric_key = 'avg_acc'

    for _ in train_iterator:
        epoch_iterator = tqdm(train_dataloader, desc="Iteration", disable=args.local_rank not in [-1, 0])
        for step, batch in enumerate(epoch_iterator):

            # Skip past any already trained steps if resuming training
            if steps_trained_in_current_epoch > 0:
                steps_trained_in_current_epoch -= 1
                continue

            model.train()
            batch = tuple(t.to(args.device) for t in batch)
            inputs = {
                "input_ids": batch[0],
                "attention_mask": batch[1],
                "labels": batch[3],
            }
            inputs["token_type_ids"] = batch[2]
            outputs = model(**inputs)
            loss = outputs[0]  # model outputs are always tuple in transformers (see doc)

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu parallel training
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps

            if args.fp16:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()    

            tr_loss += loss.item()

            if (step + 1) % args.gradient_accumulation_steps == 0:
                if args.fp16:
                    torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), args.max_grad_norm)
                else:
                    torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)

                optimizer.step()
                scheduler.step()  # Update learning rate schedule
                model.zero_grad()
                global_step += 1

                if args.local_rank in [-1, 0] and args.logging_steps > 0 and global_step % args.logging_steps == 0:
                    logs = {}
                    keep_best_step += 1
                    if (
                            args.local_rank == -1 and args.evaluate_during_training
                    ):  # Only evaluate when single GPU otherwise metrics may not average well
                        results = evaluate_glue(args, model, tokenizer)
                        res_for_display = {}
                        num_metric = 0
                        avg_metric = 0
                        for k, v in results.items():
                            num_metric += 1
                            avg_metric += v
                            res_for_display[k.replace("-", "_")] = v
                        if args.task_name == 'mnli':
                            results[metric_key] = avg_metric / num_metric
                            res_for_display[metric_key] = avg_metric / num_metric
                        fitlog.add_metric({"dev": res_for_display}, step=global_step)
                        if results[metric_key] > best:
                            keep_best_step = 0
                            best = results[metric_key]
                            best_all_metric.update(results)
                            fitlog.add_best_metric({"dev": {metric_key.replace("-", "_"): best}})
                            # save the best model
                            if not args.not_save_model:
                                output_dir = os.path.join(args.output_dir, "best_model")
                                model_to_save = (
                                    model.module if hasattr(model, "module") else model
                                )  # Take care of distributed/parallel training
                                model_to_save.save_pretrained(output_dir)
                                tokenizer.save_pretrained(output_dir)

                                torch.save(args, os.path.join(output_dir, "training_args.bin"))
                                logger.info("Saving model checkpoint to %s", output_dir)

                                torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                                torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                                logger.info("Saving optimizer and scheduler states to %s", output_dir)


                        for key, value in results.items():
                            eval_key = "eval_{}".format(key)
                            logs[eval_key] = value


                    loss_scalar = (tr_loss - logging_loss) / args.logging_steps
                    learning_rate_scalar = scheduler.get_lr()[0]
                    logs["learning_rate"] = learning_rate_scalar
                    logs["loss"] = loss_scalar
                    logging_loss = tr_loss

                    fitlog.add_loss(loss_scalar, name="Loss", step=global_step)

                    print(json.dumps({**logs, **{"step": global_step}}))

                    if keep_best_step >= args.early_stop_steps:
                        epoch_iterator.close()
                        break

            if args.max_steps > 0 and global_step > args.max_steps:
                epoch_iterator.close()
                break

        if (
                args.local_rank == -1 and args.evaluate_during_training and args.logging_steps == 0
        ):
            keep_best_step += 1
            logs = {}
            results = evaluate_glue(args, model, tokenizer)
            res_for_display = {}
            for k, v in results.items():
                res_for_display[k.replace("-", "_")] = v
            fitlog.add_metric({"dev": res_for_display}, step=global_step)
            if results[metric_key] > best:
                keep_best_step = 0
                best = results[metric_key]
                best_all_metric.update(results)
                fitlog.add_best_metric({"dev": {metric_key.replace("-", "_"): best}}) 
                # save the best model
                if not args.not_save_model:
                    output_dir = os.path.join(args.output_dir, "best_model")
                    model_to_save = (
                        model.module if hasattr(model, "module") else model
                    )  # Take care of distributed/parallel training
                    model_to_save.save_pretrained(output_dir)
                    tokenizer.save_pretrained(output_dir)

                    torch.save(args, os.path.join(output_dir, "training_args.bin"))
                    logger.info("Saving model checkpoint to %s", output_dir)

                    torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                    torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                    logger.info("Saving optimizer and scheduler states to %s", output_dir)

            for key, value in results.items():
                eval_key = "eval_{}".format(key)
                logs[eval_key] = value

            learning_rate_scalar = scheduler.get_lr()[0]
            logs["learning_rate"] = learning_rate_scalar

            print(json.dumps({**logs, **{"step": global_step}}))

        if keep_best_step >= args.early_stop_steps:
            train_iterator.close()
            logging.info("The task stops early at step {}.".format(global_step))
            break

        if args.max_steps > 0 and global_step > args.max_steps:
            train_iterator.close()
            break

    logs = {}
    if (
            args.local_rank == -1 and args.evaluate_during_training and args.logging_steps > 0 and
             global_step % args.logging_steps != 0 and keep_best_step < args.early_stop_steps
    ):
        results = evaluate_glue(args, model, tokenizer)
        res_for_display = {}
        for k, v in results.items():
            res_for_display[k.replace("-", "_")] = v
        fitlog.add_metric({"dev": res_for_display}, step=global_step)
        if results[metric_key] > best:
            best = results[metric_key]
            best_all_metric.update(results)
            fitlog.add_best_metric({"dev": {metric_key.replace("-", "_"): best}}) 
            # save the best model
            if not args.not_save_model:
                output_dir = os.path.join(args.output_dir, "best_model")
                model_to_save = (
                    model.module if hasattr(model, "module") else model
                )  # Take care of distributed/parallel training
                model_to_save.save_pretrained(output_dir)
                tokenizer.save_pretrained(output_dir)

                torch.save(args, os.path.join(output_dir, "training_args.bin"))
                logger.info("Saving model checkpoint to %s", output_dir)

                torch.save(optimizer.state_dict(), os.path.join(output_dir, "optimizer.pt"))
                torch.save(scheduler.state_dict(), os.path.join(output_dir, "scheduler.pt"))
                logger.info("Saving optimizer and scheduler states to %s", output_dir)

        for key, value in results.items():
            eval_key = "eval_{}".format(key)
            logs[eval_key] = value

        learning_rate_scalar = scheduler.get_lr()[0]
        logs["learning_rate"] = learning_rate_scalar

        print(json.dumps({**logs, **{"step": global_step}}))

    if args.local_rank in [-1, 0]:
        fitlog.finish()

    return global_step, tr_loss / global_step, best_all_metric


def main():
    args = get_args()

    if not os.path.exists(args.log_dir):
        try:
            os.makedirs(args.log_dir)
        except:
            pass


    if (
            os.path.exists(args.output_dir)
            and os.listdir(args.output_dir)
            and args.do_train
            and not args.overwrite_output_dir
    ):
        raise ValueError(
            "Output directory ({}) already exists and is not empty. Use --overwrite_output_dir to overcome.".format(
                args.output_dir
            )
        )

    # Setup distant debugging if needed
    if args.server_ip and args.server_port:
        # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
        import ptvsd

        print("Waiting for debugger attach")
        ptvsd.enable_attach(address=(args.server_ip, args.server_port), redirect_output=True)
        ptvsd.wait_for_attach() 

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend="nccl")
        args.n_gpu = 1
    args.device = device

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN,
    )

    logger.warning(
        "Process rank: %s, device: %s, n_gpu: %s, distributed training: %s, 16-bits training: %s",
        args.local_rank,
        device,
        args.n_gpu,
        bool(args.local_rank != -1),
        args.fp16,
    )

    # Set the verbosity to info of the Transformers logger (on main process only):
    if is_main_process(args.local_rank):
        transformers.utils.logging.set_verbosity_info()
        transformers.utils.logging.enable_default_handler()
        transformers.utils.logging.enable_explicit_format()
    # Set seed
    set_seed(args)

    # Prepare GLUE task
    args.task_name = args.task_name.lower()
    if args.task_name not in processors:
        raise ValueError("Task not found: %s" % (args.task_name))
    processor = processors[args.task_name]()
    args.output_mode = output_modes[args.task_name]
    label_list = processor.get_labels()
    num_labels = len(label_list)


    # Load pretrained model and tokenizer
    if args.local_rank not in [-1, 0]:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    config = ElasticBertConfig.from_pretrained(        
        args.model_name_or_path,
        num_labels=num_labels,
        finetuning_task=args.task_name,
        num_hidden_layers=args.num_hidden_layers,
        num_output_layers=args.num_output_layers,
        cache_dir=args.cache_dir if args.cache_dir else None,
    )

    tokenizer = ElasticBertTokenizer.from_pretrained(
        args.model_name_or_path,
        do_lower_case=args.do_lower_case,
        cache_dir=args.cache_dir if args.cache_dir else None,        
    )

    model = ElasticBertForSequenceClassification.from_pretrained(
        args.model_name_or_path, 
        config=config, 
        add_pooling_layer=True,
    )


    if args.local_rank == 0:
        torch.distributed.barrier()  # Make sure only the first process in distributed training will download model & vocab

    model.to(args.device)  

    print("Total Model Parameters:", sum(param.numel() for param in model.parameters()))

    logger.info("Training/evaluation parameters %s", args)

    train_dataset = None
    best_all_metric = None
    if args.do_train:
        train_dataset = load_and_cache_examples_glue(args, args.task_name, tokenizer, data_type='train')
        global_step, tr_loss, best_all_metric = train(args, train_dataset, model, tokenizer)
        logger.info(" global_step = %s, average loss = %s", global_step, tr_loss)

    if args.do_infer:
        best_model_path = os.path.join(args.output_dir, "best_model")
        if os.path.exists(best_model_path):

            model = ElasticBertForSequenceClassification.from_pretrained(best_model_path)
            model.to(args.device) 
            inference_glue(args, model, tokenizer)
        else:
            raise Exception("There is no best model path.")     
      
    return best_all_metric


if __name__ == "__main__":
    best = main()
