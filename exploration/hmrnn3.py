from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import rnn_cell_impl
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python import debug as tf_debug
import tensorflow as tf
import collections

num_batches = 10
batch_size = 2
truncate_len = 10


HMLSTMState = collections.namedtuple('HMLSTMCellState', ('c', 'h', 'z'))


class HMLSTMCell(rnn_cell_impl.RNNCell):
    def __init__(self, num_units, batch_size):
        super(HMLSTMCell, self).__init__(_reuse=None)
        self._num_units = num_units
        self._batch_size = batch_size

    @property
    def state_size(self):
        # the state is c, h, and z
        return (self._num_units, self._num_units, 1)

    @property
    def output_size(self):
        # outputs h and z
        return self._num_units + 1

    def zero_state(self, batch_size, dtype):
        return HMLSTMState(
            c=tf.zeros([batch_size, self._num_units]),
            h=tf.zeros([batch_size, self._num_units]),
            z=tf.zeros([batch_size]))

    def call(self, inputs, state):
        """Hierarchical multi-scale long short-term memory cell (HMLSTM)"""
        c, h, z = state

        in_splits = tf.constant([self._num_units, 1, self._num_units])
        hb, zb, ha = array_ops.split(
            value=inputs, num_or_size_splits=in_splits, axis=2, name='split_input')

        s_recurrent = h
        expanded_z = tf.expand_dims(tf.expand_dims(z, -1), -1)
        s_above = tf.squeeze(tf.multiply(expanded_z, ha), axis=1)
        s_below = tf.squeeze(tf.multiply(zb, hb), axis=1)

        length = 4 * self._num_units + 1
        states = [s_recurrent, s_above, s_below]
        concat = rnn_cell_impl._linear(states, length, bias=True)

        gate_splits = tf.constant(
            ([self._num_units] * 4) + [1], dtype=tf.int32)

        i, g, f, o, z_tilde = array_ops.split(
            value=concat, num_or_size_splits=gate_splits, axis=1)

        new_c = self.calculate_new_cell_state(c, g, i, f, z, zb)
        new_h = self.calculate_new_hidden_state(h, o, new_c, z, zb)
        new_z = self.calculate_new_indicator(z_tilde)

        output = array_ops.concat((new_h, tf.expand_dims(new_z, -1)), axis=1)
        new_state = HMLSTMState(new_c, new_h, new_z)

        return output, new_state

    def calculate_new_cell_state(self, c, g, i, f, z, zb):
        # update c and h according to correct operations
        # must do each batch independently
        new_c = [0] * self._batch_size
        for b in range(self._batch_size):

            def copy_c():
                return c[b]

            def update_c():
                return tf.add(tf.multiply(f[b], c[b]), tf.multiply(i[b], g[b]))

            def flush_c():
                return tf.multiply(i[b], g[b], name='c')

            new_c[b] = tf.case(
                [
                    (tf.equal(
                        tf.squeeze(z[b]), tf.constant(1., dtype=tf.float32)),
                     flush_c),
                    (tf.logical_and(
                        tf.equal(
                            tf.squeeze(z[b]), tf.constant(
                                0., dtype=tf.float32)),
                        tf.equal(
                            tf.squeeze(zb[b]),
                            tf.constant(0., dtype=tf.float32))), copy_c),
                    (tf.logical_and(
                        tf.equal(
                            tf.squeeze(z[b]), tf.constant(
                                0., dtype=tf.float32)),
                        tf.equal(
                            tf.squeeze(zb[b]),
                            tf.constant(1., dtype=tf.float32))), update_c),
                ],
                default=update_c,
                exclusive=True)

        return tf.stack(new_c, axis=0)

    def calculate_new_hidden_state(self, h, o, new_c, z, zb):
        new_h = [0] * self._batch_size
        for b in range(self._batch_size):

            def copy_h():
                return h[b]

            def update_h():
                return tf.multiply(o[b], tf.tanh(new_c[b]))

            new_h[b] = tf.cond(
                tf.logical_and(
                    tf.equal(
                        tf.squeeze(z[b]), tf.constant(0., dtype=tf.float32)),
                    tf.equal(
                        tf.squeeze(zb[b]), tf.constant(0., dtype=tf.float32))),
                copy_h, update_h)
        return tf.stack(new_h, axis=0)

    def calculate_new_indicator(self, z_tilde):
        for i in range(batch_size):
            tf.summary.scalar('z_tilde' + str(i), tf.squeeze(z_tilde[i]))
        # use slope annealing trick
        slope_multiplier = 1  # tf.maximum(tf.constant(.02) + self.epoch, tf.constant(5.))
        sigmoided = tf.sigmoid(z_tilde) * slope_multiplier

        # replace gradient calculation - use straight-through estimator
        # see: https://r2rt.com/binary-stochastic-neurons-in-tensorflow.html
        graph = tf.get_default_graph()
        with ops.name_scope('BinaryRound') as name:
            with graph.gradient_override_map({'Round': 'Identity'}):
                new_z = tf.round(sigmoided, name=name)

        return tf.squeeze(new_z, axis=1)


class MultiHMLSTMCell(rnn_cell_impl.RNNCell):
    """HMLSTM cell composed squentially of individual HMLSTM cells."""

    def __init__(self, cells):
        super(MultiHMLSTMCell, self).__init__(_reuse=None)
        self._cells = cells

    def zero_state(self, batch_size, dtype):
        name = type(self).__name__ + 'ZeroState'
        with ops.name_scope(name, values=[batch_size]):
            return tuple(
                cell.zero_state(batch_size, dtype) for cell in self._cells)

    @property
    def state_size(self):
        return tuple(cell.state_size for cell in self._cells)

    @property
    def output_size(self):
        return self._cells[-1].output_size

    def call(self, inputs, state):
        """Run this multi-layer cell on inputs, starting from state."""
        raw_inp = inputs[:, :, :-sum(c._num_units for c in self._cells)]

        # split out the part of the input that stores values of ha
        raw_h_aboves = inputs[:, :, -sum(c._num_units for c in self._cells):]
        h_aboves = array_ops.split(value=raw_h_aboves,
                                   num_or_size_splits=len(self._cells), axis=2)

        z_below = tf.ones([tf.shape(inputs)[0], 1, 1])

        raw_inp = array_ops.concat([raw_inp, z_below], axis=2)

        new_states = []
        for i, cell in enumerate(self._cells):
            with vs.variable_scope("cell_%d" % i):
                cur_state = state[i]

                print(i)
                cur_inp = array_ops.concat([raw_inp, h_aboves[i]], axis=2, name='input_to_cell')
                raw_inp, new_state = cell(cur_inp, cur_state)
                raw_inp = tf.expand_dims(raw_inp, 1)
                new_states.append(new_state)

        hidden_states = [ns[1] for ns in new_states]
        return hidden_states, tuple(new_states)


class MultiHMLSTMNetwork(object):
    def __init__(self, batch_size, num_layers, truncate_len, num_units):
        self._out_hidden_size = 100
        self._embed_size = 100
        self._batch_size = batch_size
        self._num_layers = num_layers
        self._truncate_len = truncate_len
        self._num_units = num_units  # the length of c and h

        batch_shape = (batch_size, truncate_len, num_units)
        self.batch_in = tf.placeholder(
            tf.float32, shape=batch_shape, name='batch_in')

        self.batch_out = tf.placeholder(
            tf.int32, shape=batch_shape, name='batch_out')

        self._initialize_output_variables()
        self._initialize_gate_variables()

        self.train, self.loss, self.indicators, self.predictions = self.create_network(
            self.create_output_module)

    def _initialize_gate_variables(self):
        with vs.variable_scope('gates'):
            for l in range(self._num_layers):
                vs.get_variable(
                    'gate_%s' % l, [self._num_units * self._num_layers, 1],
                    dtype=tf.float32)

    def _initialize_output_variables(self):
        with vs.variable_scope('output_module'):
            vs.get_variable('b1', [1, self._out_hidden_size], dtype=tf.float32)
            vs.get_variable('b2', [1, self._out_hidden_size], dtype=tf.float32)
            vs.get_variable('b3', [1, self._num_units], dtype=tf.float32)
            vs.get_variable(
                'w1', [self._embed_size, self._out_hidden_size], dtype=tf.float32)
            vs.get_variable(
                'w2', [self._out_hidden_size, self._out_hidden_size], dtype=tf.float32)
            vs.get_variable(
                'w3', [self._out_hidden_size, self._num_units], dtype=tf.float32)
            embed_shape = [self._num_layers * self._num_units, self._embed_size]
            vs.get_variable('embed_weights', embed_shape, dtype=tf.float32)

    def gate_input(self, hidden_states):
        # gate the incoming hidden states
        with vs.variable_scope('gates', reuse=True):
            gates = []
            for l in range(self._num_layers):
                weights = vs.get_variable(
                    'gate_%s' % l, [self._num_units * self._num_layers, 1],
                    dtype=tf.float32)
                gates.append(tf.sigmoid(tf.matmul(hidden_states, weights)))

            split = array_ops.split(
                value=hidden_states,
                num_or_size_splits=self._num_layers,
                axis=1)
            gated_list = []
            for gate, hidden_state in zip(gates, split):
                gated_list.append(tf.multiply(gate, hidden_state))

            gated_input = tf.concat(gated_list, axis=1)
        return gated_input

    def create_output_module(self, gated_input, time_step):
        with vs.variable_scope('output_module', reuse=True):

            in_size = self._num_layers * self._num_units
            embed_shape = [in_size, self._embed_size]
            embed_weights = vs.get_variable(
                'embed_weights', embed_shape, dtype=tf.float32)

            b1 = vs.get_variable('b1', [1, self._out_hidden_size], dtype=tf.float32)
            b2 = vs.get_variable('b2', [1, self._out_hidden_size], dtype=tf.float32)
            b3 = vs.get_variable('b3', [1, self._num_units], dtype=tf.float32)
            w1 = vs.get_variable(
                'w1', [self._embed_size, self._out_hidden_size], dtype=tf.float32)
            w2 = vs.get_variable(
                'w2', [self._out_hidden_size, self._out_hidden_size], dtype=tf.float32)
            w3 = vs.get_variable(
                'w3', [self._out_hidden_size, self._num_units], dtype=tf.float32)

            # embedding
            prod = tf.matmul(gated_input, embed_weights)
            embedding = tf.nn.relu(prod)

            # feed forward network
            # first layer
            l1 = tf.nn.tanh(tf.matmul(embedding, w1) + b1)

            # second layer
            l2 = tf.nn.tanh(tf.matmul(l1, w2) + b2)

            # the loss function used below
            # softmax_cross_entropy_with_logits
            prediction = tf.matmul(l2, w3) + b3

            loss = tf.nn.softmax_cross_entropy_with_logits(
                labels=self.batch_out[:, time_step:time_step + 1, :],
                logits=prediction,
                name='loss'
            )
            scalar_loss = tf.reduce_mean(loss, name='loss_mean')
        return scalar_loss, prediction

    def create_network(self, output_module):
        def hmlstm_cell():
            return HMLSTMCell(self._num_units, self._batch_size)

        hmlstm = MultiHMLSTMCell(
            [hmlstm_cell() for _ in range(self._num_layers)])

        state = hmlstm.zero_state(self._batch_size, tf.float32)
        ha_shape = [self._batch_size, 1, (self._num_layers * self._num_units)]
        h_aboves = tf.zeros(ha_shape)

        loss = tf.constant(0.0)
        indicators = []
        predictions = []
        for i in range(self._truncate_len):
            inputs = array_ops.concat(
                (self.batch_in[:, i:(i + 1), :], h_aboves), axis=2)

            hidden_states, state = hmlstm(inputs, state)
            concated_hs = array_ops.concat(hidden_states[1:], axis=1)
            h_aboves = array_ops.concat(
                [
                    concated_hs, tf.zeros(
                        [self._batch_size, self._num_units], dtype=tf.float32)
                ],
                axis=1)
            h_aboves = tf.expand_dims(h_aboves, 1)

            gated = self.gate_input(array_ops.concat(hidden_states, axis=1))
            new_loss, new_prediction = output_module(gated, i)
            loss += tf.reduce_mean(new_loss)

            predictions.append(new_prediction)
            indicators += [z for c, h, z in state]

        train = tf.train.AdamOptimizer(1e-3).minimize(loss)
        return train, loss, indicators, predictions

    def train(self, batches_in, batches_out, epochs=10):
        writer = tf.summary.FileWriter('./log/', tf.get_default_graph())
        summary_ops = tf.summary.merge_all()
        with tf.Session() as sess:
            init = tf.global_variables_initializer()
            sess.run(init)
            # Debugging
            # wrapped_sess = tf_debug.LocalCLIDebugWrapperSession(sess)

            # def input_filter(debug_info, values):
            #     return 'input_to_cell' in debug_info.node_name

            # def split_filter(debug_info, values):
            #     return 'split_input' in debug_info.node_name

            # wrapped_sess.add_tensor_filter('input_filter', input_filter)
            # wrapped_sess.add_tensor_filter('split_input', split_filter)


            for epoch in range(epochs):
                print('new epoch')
                for batch_in, batch_out in zip(batches_in, batches_out):
                    _results = sess.run(
                        [summary_ops, self.train, self.loss] + self.indicators, {
                        self.batch_in: batch_in,
                        self.batch_out: batch_out,
                    })
                    writer.add_summary(_results[0])
                    print(_results)

    def predict(self):
        pass

    def predict_boundaries(self):
        pass

from string import ascii_lowercase
import re
import numpy as np

def text():
    signals = load_text()
    print(signals)
    hot = [(one_hot_encode(intext), one_hot_encode(outtext))
           for intext, outtext in signals]
    return hot


def load_text():
    with open('text.txt', 'r') as f:
        text = f.read()
        text = text.replace('\n', ' ')
        text = re.sub(' +', ' ', text)

    # text = 'abcdefghijklmnopqrstuvwxyz' * 100000000

    signals = []
    start = 0
    for _ in range(batch_size * num_batches):
        intext = text[start:start + truncate_len]
        outtext = text[start + 1:start + truncate_len + 1]
        signals.append((intext, outtext))
        start += truncate_len

    return signals


def one_hot_encode(text):
    out = np.zeros((len(text), 29))

    def get_index(char):
        answers = {',': 26, '.': 27}

        if char in answers:
            return answers[char]

        try:
            return ascii_lowercase.index(char)
        except:
            return 28

    for i, t in enumerate(text):
        out[i, get_index(t)] = 1

    return out


def prepare_inputs():
    y = text()
    batches_in = []
    batches_out = []

    for batch_number in range(num_batches):
        start = batch_number * batch_size
        end = start + batch_size
        batches_in.append([i for i, _ in y[start:end]])
        batches_out.append([o for _, o in y[start:end]])

    return (batches_in, batches_out)



def get_network():
    return MultiHMLSTMNetwork(batch_size, 3, truncate_len, 29)


def run_everything():
    inputs = prepare_inputs()
    network = get_network()
    network.run(*inputs)


if __name__ == '__main__':
    run_everything()
