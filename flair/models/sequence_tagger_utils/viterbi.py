import numpy
import torch
import torch.nn
from torch.nn.functional import softmax
from torch.nn.utils.rnn import pack_padded_sequence

import flair
from flair.data import Dictionary, Label, List

from .utils import log_sum_exp, START_TAG, STOP_TAG


class ViterbiLoss(torch.nn.Module):
    """
    Viterbi Loss according to sgrvinod (https://github.com/sgrvinod).
    Calculates the loss for each sequence up to its length t.
    """

    def __init__(self, tag_dictionary: Dictionary):
        """
        :param tag_dictionary: tag_dictionary of task
        """
        super(ViterbiLoss, self).__init__()
        self.tag_dictionary = tag_dictionary
        self.tagset_size = len(tag_dictionary)
        self.start_tag = tag_dictionary.get_idx_for_item(START_TAG)
        self.stop_tag = tag_dictionary.get_idx_for_item(STOP_TAG)

    def forward(self, features_tuple: tuple, targets: torch.tensor) -> torch.tensor:
        """
        Forward propagation of Viterbi Loss

        :param features: CRF scores from CRF forward method in shape (batch size, seq len, tagset size, tagset size)
        :param targets: true tags for sentences which come in as matrix indices.
            CRF scores contain per sentence, per token a (tagset_size x tagset_size) matrix, containing emission score for
            token j + transition prob from previous token i. Means, if we think of our rows as "from tag" and our columns
            as "to tag", the matrix in cell [10,5] would contain the emission score for tag 5 + transition score
            from previous tag 10 and could directly be addressed through the 1-dim indices (10 * tagset_size + 5) = 125,
            if our tagset consists of 12 tags.
        :param lengths: lengths tuple containing sorted lengths and indices from unsorted list
        :return: average Viterbi Loss over batch size
        """
        features, lengths = features_tuple
        batch_size = features.size(0)
        seq_len = features.size(1)

        formatted_targets = self.format_targets(targets, lengths)

        targets = torch.tensor(formatted_targets, dtype=torch.long).unsqueeze(2).to(flair.device)

        # Squeeze crf scores matrices in 1-dim shape and gather scores at targets by matrix indices
        scores_at_targets = torch.gather(features.view(batch_size, seq_len, -1), 2, targets)
        scores_at_targets = pack_padded_sequence(scores_at_targets, lengths.values, batch_first=True)[0]
        gold_score = scores_at_targets.sum()

        scores_upto_t = torch.zeros(batch_size, self.tagset_size, device=flair.device)

        for t in range(max(lengths.values)):
            batch_size_t = sum([l > t for l in lengths.values])  # since batch is ordered, we can save computation time by reducing our effective batch_size

            if t == 0:
                # Initially, get scores from <start> tag to all other tags
                scores_upto_t[:batch_size_t] = features[:batch_size_t, t, self.start_tag, :]
            else:
                # We add scores at current timestep to scores accumulated up to previous timestep, and log-sum-exp
                # Remember, the cur_tag of the previous timestep is the prev_tag of this timestep
                scores_upto_t[:batch_size_t] = log_sum_exp(features[:batch_size_t, t, :, :] + scores_upto_t[:batch_size_t].unsqueeze(2), dim=1)

        all_paths_scores = scores_upto_t[:, self.stop_tag].sum()

        viterbi_loss = all_paths_scores - gold_score

        return viterbi_loss

    def format_targets(self, targets: torch.tensor, lengths: torch.tensor):
        targets_per_sentence = []

        targets_list = targets.tolist()
        for cut in lengths.values:
            targets_per_sentence.append(targets_list[:cut])
            targets_list = targets_list[cut:]

        for t in targets_per_sentence:
            t += [self.tag_dictionary.get_idx_for_item("<unk>")] * (max(lengths.values) - len(t))

        tmaps = list(map(lambda s: [self.tag_dictionary.get_idx_for_item(START_TAG) * self.tagset_size + s[0]] + [s[i - 1] * self.tagset_size + s[i] for i in range(1, len(s))],
                         targets_per_sentence))

        return tmaps

class ViterbiDecoder:
    """
    Viterbi Decoder according to sgrvinod (https://github.com/sgrvinod).
    Decodes a given sequence using the Viterbi algorithm.
    """

    def __init__(self, tag_dictionary: Dictionary):
        """
        :param tag_dictionary: Dictionary of tags for sequence labeling task
        """
        self.tag_dictionary = tag_dictionary
        self.tagset_size = len(tag_dictionary)
        self.start_tag = tag_dictionary.get_idx_for_item(START_TAG)
        self.stop_tag = tag_dictionary.get_idx_for_item(STOP_TAG)

    def decode(self, features_tuple: tuple, transitions: numpy.array) -> List:
        """
        Decoding function returning the most likely sequence of tags.
        :param features: CRF scores from CRF forward method in shape (batch size, seq len, tagset size, tagset size)
        :param lengths: lengths tuple containing sorted lengths and indices from unsorted list
        :return: decoded sequences
        """
        features, lengths = features_tuple

        tags = []
        batch_size = features.size(0)
        seq_len = features.size(1)

        # Create a tensor to hold accumulated sequence scores at each current tag
        scores_upto_t = torch.zeros(batch_size, seq_len + 1, self.tagset_size).to(flair.device)
        # Create a tensor to hold back-pointers
        # i.e., indices of the previous_tag that corresponds to maximum accumulated score at current tag
        # Let pads be the <end> tag index, since that was the last tag in the decoded sequence
        backpointers = torch.ones((batch_size, seq_len + 1, self.tagset_size), dtype=torch.long,
                                  device=flair.device) * self.stop_tag

        start_forward_var = torch.ones([self.tagset_size]).to(flair.device) * -10000
        start_forward_var[self.tag_dictionary.get_idx_for_item(START_TAG)] = 0
        start_forward_var = start_forward_var + transitions

        # initial fill of forward var
        scores_upto_t[:, 0], backpointers[:, 0] = torch.max(start_forward_var, dim=1)

        for t in range(seq_len):
            batch_size_t = sum([l > t for l in lengths.values])  # effective batch size (sans pads) at this timestep
            terminate_var = [idx for idx, l in enumerate(lengths.values) if l == t + 1]

            # We add scores at current timestep to scores accumulated up to previous timestep, and
            # choose the previous timestep that corresponds to the max. accumulated score for each current timestep
            scores_upto_t[:batch_size_t, t + 1], backpointers[:batch_size_t, t + 1, :] = torch.max(
                features[:batch_size_t, t, :, :] + scores_upto_t[:batch_size_t, t].unsqueeze(1),
                dim=2)

            # if sentence is completed, add transition to stop tag
            if terminate_var:
                scores_upto_t[terminate_var, t + 1, self.tag_dictionary.get_idx_for_item(START_TAG)] = -10000.0
                scores_upto_t[terminate_var, t + 1, self.tag_dictionary.get_idx_for_item(STOP_TAG)] = -10000.0

        """
        best_tag_id = terminal_var.argmax()

        best_path = [best_tag_id]
        for bptrs_t in reversed(backpointers):
            best_tag_id = bptrs_t[best_tag_id]
            best_path.append(best_tag_id)

        start = best_path.pop()
        assert start == id_start
        best_path.reverse()

        best_scores_softmax = self._softmax(backscores, axis=1)
        best_scores_np = np.max(best_scores_softmax, axis=1)
        """

        # Decode/trace best path backwards
        decoded = torch.zeros((batch_size, backpointers.size(1)), dtype=torch.long, device=flair.device)
        pointer = torch.ones((batch_size, 1), dtype=torch.long, device=flair.device) * self.stop_tag

        for t in list(reversed(range(backpointers.size(1)))):
            decoded[:, t] = torch.gather(backpointers[:, t, :], 1, pointer).squeeze(1)
            pointer = decoded[:, t].unsqueeze(1)

        # Sanity check
        assert torch.equal(decoded[:, 0], torch.ones((batch_size), dtype=torch.long, device=flair.device) * self.start_tag)

        # Max + Softmax to get confidence score for predicted label and append label to each token
        confidences = torch.max(softmax(scores_upto_t, dim=2), dim=2)

        for tag_seq, tag_seq_conf, length_seq in zip(decoded, confidences.values, lengths.values):
            tags.append(
                [
                    Label(self.tag_dictionary.get_item_for_index(tag), conf.item())
                    for tag, conf in list(zip(tag_seq, tag_seq_conf))[1:length_seq + 1]
                ]
            )

        return tags