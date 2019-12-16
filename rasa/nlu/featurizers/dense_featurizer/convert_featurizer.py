import logging
import warnings
from rasa.nlu.featurizers.featurzier import Featurizer
from typing import Any, Dict, List, Optional, Text, Tuple
from rasa.nlu.config import RasaNLUModelConfig
from rasa.nlu.training_data import Message, TrainingData
from rasa.nlu.constants import (
    TEXT_ATTRIBUTE,
    TOKENS_NAMES,
    DENSE_FEATURE_NAMES,
    DENSE_FEATURIZABLE_ATTRIBUTES,
    CLS_TOKEN,
)
import numpy as np
import tensorflow as tf

logger = logging.getLogger(__name__)


class ConveRTFeaturizer(Featurizer):

    provides = [
        DENSE_FEATURE_NAMES[attribute] for attribute in DENSE_FEATURIZABLE_ATTRIBUTES
    ]

    defaults = {
        # if True return a sequence of features (return vector has size
        # token-size x feature-dimension)
        # if False token-size will be equal to 1
        "return_sequence": False
    }

    def _load_model(self) -> None:

        # needed in order to load model
        import tensorflow_text
        import tensorflow_hub as tfhub

        self.graph = tf.Graph()
        model_url = "http://models.poly-ai.com/convert/v1/model.tar.gz"

        with self.graph.as_default():
            self.session = tf.Session()
            self.module = tfhub.Module(model_url)

            self.text_placeholder = tf.placeholder(dtype=tf.string, shape=[None])
            self.sentence_encoding_tensor = self.module(self.text_placeholder)
            if self.return_sequence:
                self.sequence_encoding_tensor = self.module(
                    self.text_placeholder, signature="encode_sequence", as_dict=True
                )
            self.session.run(tf.tables_initializer())
            self.session.run(tf.global_variables_initializer())

    def __init__(self, component_config: Optional[Dict[Text, Any]] = None) -> None:

        super(ConveRTFeaturizer, self).__init__(component_config)

        self._load_model()

        self.return_sequence = self.component_config["return_sequence"]

    @classmethod
    def required_packages(cls) -> List[Text]:
        return ["tensorflow_text", "tensorflow_hub"]

    def _compute_features(
        self, batch_examples: List[Message], attribute: Text = TEXT_ATTRIBUTE
    ) -> np.ndarray:

        sentence_encodings = self._compute_sentence_encodings(batch_examples, attribute)

        if not self.return_sequence:
            return sentence_encodings

        return self._compute_sequence_encodings(
            batch_examples, sentence_encodings, attribute
        )

    def _compute_sentence_encodings(
        self, batch_examples: List[Message], attribute: Text = TEXT_ATTRIBUTE
    ) -> np.ndarray:
        # Get text for attribute of each example
        batch_attribute_text = [ex.get(attribute) for ex in batch_examples]
        sentence_encodings = self._sentence_encoding_of_text(batch_attribute_text)

        # convert them to a sequence of 1
        return np.reshape(sentence_encodings, (len(batch_examples), 1, -1))

    def _compute_sequence_encodings(
        self,
        batch_examples: List[Message],
        sentence_encodings: np.ndarray,
        attribute: Text = TEXT_ATTRIBUTE,
    ) -> np.ndarray:
        cls_token_used = batch_examples[0].get(TOKENS_NAMES[attribute])[-1] == CLS_TOKEN

        final_embeddings = []

        number_of_tokens_in_sentence = [
            len(sentence.get(TOKENS_NAMES[attribute])) for sentence in batch_examples
        ]

        tokenized_text = self._tokens_to_text(batch_examples, attribute)
        sequence_encodings = self._sequence_encoding_of_text(tokenized_text)

        for index in range(len(batch_examples)):
            sequence_length = number_of_tokens_in_sentence[index]
            sequence_encoding = sequence_encodings[index][:sequence_length]
            sentence_encoding = sentence_encodings[index]

            if cls_token_used:
                # tile sequence encoding to duplicate as sentence encodings have size
                # 1024 and sequence encodings only have a dimensionality of 512
                sequence_encoding = np.tile(sequence_encoding, (1, 2))
                # add sentence encoding to the end (position of cls token)
                sequence_encoding = np.concatenate(
                    [sequence_encoding, sentence_encoding], axis=0
                )

            final_embeddings.append(sequence_encoding)

        return np.array(final_embeddings)

    @staticmethod
    def _tokens_to_text(
        batch_examples: List[Message], attribute: Text = TEXT_ATTRIBUTE
    ) -> List[Text]:
        list_of_tokens = [
            example.get(TOKENS_NAMES[attribute]) for example in batch_examples
        ]

        texts = []
        for sent_tokens in list_of_tokens:
            text = ""
            offset = 0
            for token in sent_tokens:
                if offset != token.start:
                    text += " "
                text += token.text.replace("﹏", "")

                offset += token.end - token.start
            texts.append(text)

        return texts

    def _sentence_encoding_of_text(self, batch: List[Text]) -> np.ndarray:
        return self.session.run(
            self.sentence_encoding_tensor, feed_dict={self.text_placeholder: batch}
        )

    def _sequence_encoding_of_text(self, batch: List[Text]) -> np.ndarray:
        return self.session.run(
            self.sequence_encoding_tensor, feed_dict={self.text_placeholder: batch}
        )["sequence_encoding"]

    def train(
        self,
        training_data: TrainingData,
        config: Optional[RasaNLUModelConfig],
        **kwargs: Any,
    ) -> None:

        if config is not None and config.language != "en":
            warnings.warn(
                f"Since ``ConveRT`` model is trained only on an english "
                f"corpus of conversations, this featurizer should only be "
                f"used if your training data is in english language. "
                f"However, you are training in '{config.language}'."
            )

        batch_size = 64

        for attribute in DENSE_FEATURIZABLE_ATTRIBUTES:

            non_empty_examples = list(
                filter(lambda x: x.get(attribute), training_data.training_examples)
            )

            batch_start_index = 0

            while batch_start_index < len(non_empty_examples):

                batch_end_index = min(
                    batch_start_index + batch_size, len(non_empty_examples)
                )

                # Collect batch examples
                batch_examples = non_empty_examples[batch_start_index:batch_end_index]

                batch_features = self._compute_features(batch_examples, attribute)

                for index, ex in enumerate(batch_examples):

                    ex.set(
                        DENSE_FEATURE_NAMES[attribute],
                        self._combine_with_existing_dense_features(
                            ex, batch_features[index], DENSE_FEATURE_NAMES[attribute]
                        ),
                    )

                batch_start_index += batch_size

    def process(self, message: Message, **kwargs: Any) -> None:

        features = self._compute_features([message])[0]
        message.set(
            DENSE_FEATURE_NAMES[TEXT_ATTRIBUTE],
            self._combine_with_existing_dense_features(
                message, features, DENSE_FEATURE_NAMES[TEXT_ATTRIBUTE]
            ),
        )
