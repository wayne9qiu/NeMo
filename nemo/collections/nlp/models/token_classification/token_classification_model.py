# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from typing import Dict, List, Optional, Union

import torch
from omegaconf import DictConfig, OmegaConf
from pytorch_lightning import Trainer
from torch.utils.data import DataLoader

from nemo.collections.common.losses import CrossEntropyLoss
from nemo.collections.common.tokenizers.tokenizer_utils import get_tokenizer
from nemo.collections.nlp.data.token_classification.token_classification_dataset import (
    BertTokenClassificationDataset,
    BertTokenClassificationInferDataset,
)
from nemo.collections.nlp.data.token_classification.token_classification_descriptor import TokenClassificationDataDesc
from nemo.collections.nlp.metrics.classification_report import ClassificationReport
from nemo.collections.nlp.modules.common import TokenClassifier
from nemo.collections.nlp.modules.common.common_utils import get_pretrained_lm_model
from nemo.collections.nlp.parts.utils_funcs import get_classification_report, plot_confusion_matrix, tensor2list
from nemo.core.classes.common import PretrainedModelInfo, typecheck
from nemo.core.classes.modelPT import ModelPT
from nemo.core.neural_types import NeuralType
from nemo.utils import logging

__all__ = ['TokenClassificationModel']


class TokenClassificationModel(ModelPT):
    """Token Classification Model with BERT, applicable for tasks such as Named Entity Recognition"""

    @property
    def input_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.bert_model.input_types

    @property
    def output_types(self) -> Optional[Dict[str, NeuralType]]:
        return self.classifier.output_types

    def __init__(self, cfg: DictConfig, trainer: Trainer = None):
        """Initializes Token Classification Model."""

        self.tokenizer = get_tokenizer(
            tokenizer_name=cfg.language_model.tokenizer,
            pretrained_model_name=cfg.language_model.pretrained_model_name,
            vocab_file=self.register_artifact(
                config_path='language_model.vocab_file', src=cfg.language_model.vocab_file
            ),
            tokenizer_model=self.register_artifact(
                config_path='language_model.tokenizer_model', src=cfg.language_model.tokenizer_model
            ),
            do_lower_case=cfg.language_model.do_lower_case,
        )

        self._cfg = cfg
        self.data_desc = None
        self.update_data_dir(cfg.dataset.data_dir)
        self.setup_loss(class_balancing=self._cfg.dataset.class_balancing)

        super().__init__(cfg=cfg, trainer=trainer)

        self.bert_model = get_pretrained_lm_model(
            pretrained_model_name=self._cfg.language_model.pretrained_model_name,
            config_file=self._cfg.language_model.bert_config,
            checkpoint_file=self._cfg.language_model.bert_checkpoint,
        )
        self.hidden_size = self.bert_model.hidden_size

        self.classifier = TokenClassifier(
            hidden_size=self.hidden_size,
            num_classes=len(self._cfg.label_ids),
            num_layers=self._cfg.head.num_fc_layers,
            activation=self._cfg.head.activation,
            log_softmax=self._cfg.head.log_softmax,
            dropout=self._cfg.head.fc_dropout,
            use_transformer_init=self._cfg.head.use_transformer_init,
        )

        self.loss = self.setup_loss(class_balancing=self._cfg.dataset.class_balancing)
        # setup to track metrics
        self.classification_report = ClassificationReport(len(self._cfg.label_ids), label_ids=self._cfg.label_ids)

    def update_data_dir(self, data_dir: str) -> None:
        """
        Update data directory and get data stats with Data Descriptor
        Weights are later used to setup loss

        Args:
            data_dir: path to data directory
        """
        modes = ["train", "test", "dev"]
        self._cfg.dataset.data_dir = data_dir
        logging.info(f'Setting model.dataset.data_dir to {data_dir}.')

        if os.path.exists(data_dir):
            self.data_desc = TokenClassificationDataDesc(
                data_dir=data_dir, modes=modes, pad_label=self._cfg.dataset.pad_label
            )

    def setup_loss(self, class_balancing: str = None):
        """Setup loss
           Call this method only after update_data_dir() so that self.data_desc has class weights stats

        Args:
            class_balancing: whether to use class weights during training
        """
        if class_balancing == 'weighted_loss' and self.data_desc:
            # you may need to increase the number of epochs for convergence when using weighted_loss
            loss = CrossEntropyLoss(logits_ndim=3, weight=self.data_desc.class_weights)
        else:
            loss = CrossEntropyLoss(logits_ndim=3)
        return loss

    @typecheck()
    def forward(self, input_ids, token_type_ids, attention_mask):
        hidden_states = self.bert_model(
            input_ids=input_ids, token_type_ids=token_type_ids, attention_mask=attention_mask
        )
        logits = self.classifier(hidden_states=hidden_states)
        return logits

    def training_step(self, batch, batch_idx):
        """
        Lightning calls this inside the training loop with the data from the training dataloader
        passed in as `batch`.
        """
        input_ids, input_type_ids, input_mask, subtokens_mask, loss_mask, labels = batch
        logits = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)

        loss = self.loss(logits=logits, labels=labels, loss_mask=loss_mask)
        tensorboard_logs = {'train_loss': loss, 'lr': self._optimizer.param_groups[0]['lr']}
        return {'loss': loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        """
        Lightning calls this inside the validation loop with the data from the validation dataloader
        passed in as `batch`.
        """
        input_ids, input_type_ids, input_mask, subtokens_mask, loss_mask, labels = batch
        logits = self(input_ids=input_ids, token_type_ids=input_type_ids, attention_mask=input_mask)
        val_loss = self.loss(logits=logits, labels=labels, loss_mask=loss_mask)

        subtokens_mask = subtokens_mask > 0.5

        preds = torch.argmax(logits, axis=-1)[subtokens_mask]
        labels = labels[subtokens_mask]
        tp, fp, fn = self.classification_report(preds, labels)

        tensorboard_logs = {'val_loss': val_loss, 'tp': tp, 'fn': fn, 'fp': fp}
        return {'val_loss': val_loss, 'log': tensorboard_logs}

    def validation_epoch_end(self, outputs):
        """
        Called at the end of validation to aggregate outputs.
        outputs: list of individual outputs of each validation step.
        """
        avg_loss = torch.stack([x['val_loss'] for x in outputs]).mean()

        # calculate metrics and log classification report
        tp = torch.sum(torch.stack([x['log']['tp'] for x in outputs]), 0)
        fn = torch.sum(torch.stack([x['log']['fn'] for x in outputs]), 0)
        fp = torch.sum(torch.stack([x['log']['fp'] for x in outputs]), 0)
        precision, recall, f1 = self.classification_report.get_precision_recall_f1(tp, fn, fp, mode='macro')

        tensorboard_logs = {
            'val_loss': avg_loss,
            'precision': precision,
            'f1': f1,
            'recall': recall,
        }
        return {'val_loss': avg_loss, 'log': tensorboard_logs}

    def setup_training_data(self, train_data_config: Optional[DictConfig] = None):
        if train_data_config is None:
            train_data_config = self._cfg.train_ds
        self._train_dl = self._setup_dataloader_from_config(cfg=train_data_config)

        if not torch.distributed.is_initialized() or torch.distributed.get_rank() == 0:
            self.register_artifact('label_ids.csv', self.data_desc.label_ids_filename)
            # save label maps to the config
            self._cfg.label_ids = OmegaConf.create(self.data_desc.label_ids)

    def setup_validation_data(self, val_data_config: Optional[DictConfig] = None):
        if val_data_config is None:
            val_data_config = self._cfg.validation_ds
        self._validation_dl = self._setup_dataloader_from_config(cfg=val_data_config)

    def setup_test_data(self, test_data_config: Optional[DictConfig]):
        if test_data_config is None:
            test_data_config = self._cfg.test_ds
        self._test_dl = self.__setup_dataloader_from_config(cfg=test_data_config)

    def _setup_dataloader_from_config(self, cfg: DictConfig) -> DataLoader:
        """
        Setup dataloader from config
        Args:
            cfg: config for the dataloader
        Return:
            Pytorch Dataloader
        """
        dataset_cfg = self._cfg.dataset
        data_dir = dataset_cfg.data_dir

        if not os.path.exists(data_dir):
            raise FileNotFoundError(f"Data directory is not found at: {data_dir}.")

        text_file = os.path.join(data_dir, cfg.text_file)
        labels_file = os.path.join(data_dir, cfg.labels_file)

        if not (os.path.exists(text_file) and os.path.exists(labels_file)):
            raise FileNotFoundError(
                f'{text_file} or {labels_file} not found. The data should be splitted into 2 files: text.txt and \
                labels.txt. Each line of the text.txt file contains text sequences, where words are separated with \
                spaces. The labels.txt file contains corresponding labels for each word in text.txt, the labels are \
                separated with spaces. Each line of the files should follow the format:  \
                   [WORD] [SPACE] [WORD] [SPACE] [WORD] (for text.txt) and \
                   [LABEL] [SPACE] [LABEL] [SPACE] [LABEL] (for labels.txt).'
            )
        dataset = BertTokenClassificationDataset(
            text_file=text_file,
            label_file=labels_file,
            max_seq_length=dataset_cfg.max_seq_length,
            tokenizer=self.tokenizer,
            num_samples=cfg.num_samples,
            pad_label=dataset_cfg.pad_label,
            label_ids=self.data_desc.label_ids,
            ignore_extra_tokens=dataset_cfg.ignore_extra_tokens,
            ignore_start_end=dataset_cfg.ignore_start_end,
            use_cache=dataset_cfg.use_cache,
        )

        return DataLoader(
            dataset=dataset,
            collate_fn=dataset.collate_fn,
            batch_size=cfg.batch_size,
            shuffle=cfg.shuffle,
            num_workers=dataset_cfg.num_workers,
            pin_memory=dataset_cfg.pin_memory,
            drop_last=dataset_cfg.drop_last,
        )

    def _setup_infer_dataloader(self, queries: List[str], batch_size: int) -> 'torch.utils.data.DataLoader':
        """
        Setup function for a infer data loader.

        Args:
            queries: text
            batch_size: batch size to use during inference

        Returns:
            A pytorch DataLoader.
        """

        dataset = BertTokenClassificationInferDataset(tokenizer=self.tokenizer, queries=queries, max_seq_length=-1)

        return torch.utils.data.DataLoader(
            dataset=dataset,
            collate_fn=dataset.collate_fn,
            batch_size=batch_size,
            shuffle=False,
            num_workers=self._cfg.dataset.num_workers,
            pin_memory=self._cfg.dataset.pin_memory,
            drop_last=False,
        )

    def _infer(self, queries: List[str], batch_size: int = None) -> List[int]:
        """
        Get prediction for the queries
        Args:
            queries: text sequences
            batch_size: batch size to use during inference.
        Returns:
            all_preds: model predictions
        """
        # store predictions for all queries in a single list
        all_preds = []
        mode = self.training
        try:
            device = 'cuda' if torch.cuda.is_available() else 'cpu'
            # Switch model to evaluation mode
            self.eval()
            self.to(device)
            infer_datalayer = self._setup_infer_dataloader(queries, batch_size)

            for i, batch in enumerate(infer_datalayer):
                input_ids, input_type_ids, input_mask, subtokens_mask = batch

                logits = self.forward(
                    input_ids=input_ids.to(device),
                    token_type_ids=input_type_ids.to(device),
                    attention_mask=input_mask.to(device),
                )

                subtokens_mask = subtokens_mask > 0.5
                preds = tensor2list(torch.argmax(logits, axis=-1)[subtokens_mask])
                all_preds.extend(preds)
        finally:
            # set mode back to its original value
            self.train(mode=mode)
        return all_preds

    def add_predictions(self, queries: Union[List[str], str], batch_size: int = 32) -> List[str]:
        """
        Add predicted token labels to the queries. Use this method for debugging and prototyping.
        Args:
            queries: text
            batch_size: batch size to use during inference.
        Returns:
            result: text with added entities
        """
        if queries is None or len(queries) == 0:
            return []

        result = []
        all_preds = self._infer(queries, batch_size)

        queries = [q.strip().split() for q in queries]
        num_words = [len(q) for q in queries]
        if sum(num_words) != len(all_preds):
            raise ValueError('Pred and words must have the same length')

        ids_to_labels = {v: k for k, v in self._cfg.label_ids.items()}
        start_idx = 0
        end_idx = 0
        for query in queries:
            end_idx += len(query)

            # extract predictions for the current query from the list of all predictions
            preds = all_preds[start_idx:end_idx]
            start_idx = end_idx

            query_with_entities = ''
            for j, word in enumerate(query):
                # strip out the punctuation to attach the entity tag to the word not to a punctuation mark
                # that follows the word
                if word[-1].isalpha():
                    punct = ''
                else:
                    punct = word[-1]
                    word = word[:-1]

                query_with_entities += word
                label = ids_to_labels[preds[j]]

                if label != self._cfg.dataset.pad_label:
                    query_with_entities += '[' + label + ']'
                query_with_entities += punct + ' '
            result.append(query_with_entities.strip())
        return result

    def evaluate_from_file(
        self,
        output_dir: str,
        text_file: str,
        labels_file: Optional[str] = None,
        add_confusion_matrix: Optional[bool] = False,
        normalize_confusion_matrix: Optional[bool] = True,
        batch_size: int = 32,
    ) -> List[str]:
        """
        Run inference on data from a file, plot confusion matrix and calculate classification report.
        Use this method for final evaluation.
        Args:
            output_dir: path to output directory to store model predictions, confusion matrix plot (if set to True)
            text_file: path to file with text. Each line of the text.txt file contains text sequences, where words
                are separated with spaces: [WORD] [SPACE] [WORD] [SPACE] [WORD]
            labels_file (Optional): path to file with labels. Each line of the labels_file should contain
                labels corresponding to each word in the text_file, the labels are separated with spaces:
                [LABEL] [SPACE] [LABEL] [SPACE] [LABEL] (for labels.txt).'
            add_confusion_matrix: whether to generate confusion matrix
            normalize_confusion_matrix: whether to normalize confusion matrix
            batch_size: batch size to use during inference.
        Returns:
            result: text with added capitalization and punctuation
        """
        output_dir = os.path.abspath(output_dir)

        with open(text_file, 'r') as f:
            queries = f.readlines()

        all_preds = self._infer(queries, batch_size)
        with_labels = labels_file is not None
        if with_labels:
            with open(labels_file, 'r') as f:
                all_labels_str = f.readlines()
                all_labels_str = ' '.join([labels.strip() for labels in all_labels_str])

        # writing labels and predictions to a file in output_dir is specified in the config
        os.makedirs(output_dir, exist_ok=True)
        filename = os.path.join(output_dir, 'infer_' + os.path.basename(text_file))
        with open(filename, 'w') as f:
            if with_labels:
                f.write('labels\t' + all_labels_str + '\n')
                logging.info(f'Labels save to {filename}')

            # convert labels from string label to ids
            ids_to_labels = {v: k for k, v in self._cfg.label_ids.items()}
            all_preds_str = [ids_to_labels[pred] for pred in all_preds]
            f.write('preds\t' + ' '.join(all_preds_str) + '\n')
            logging.info(f'Predictions saved to {filename}')

        if with_labels and add_confusion_matrix:
            all_labels = all_labels_str.split()
            # convert labels from string label to ids
            label_ids = self._cfg.label_ids
            all_labels = [label_ids[label] for label in all_labels]
            print(len(all_labels), len(all_preds))
            plot_confusion_matrix(
                all_labels, all_preds, output_dir, label_ids=label_ids, normalize=normalize_confusion_matrix
            )
            logging.info(get_classification_report(all_labels, all_preds, label_ids))

    @classmethod
    def list_available_models(cls) -> Optional[PretrainedModelInfo]:
        """
        This method returns a list of pre-trained model which can be instantiated directly from NVIDIA's NGC cloud.

        Returns:
            List of available pre-trained models.
        """
        result = []
        model = PretrainedModelInfo(
            pretrained_model_name="NERModel",
            location="https://nemo-public.s3.us-east-2.amazonaws.com/nemo-1.0.0alpha-tests/ner.nemo",
            description="The model was trained on GMB (Groningen Meaning Bank) corpus for entity recognition and "
            + "achieves 74.61 F1 Macro score.",
        )
        result.append(model)
        return result
