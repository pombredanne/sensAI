import functools
import io
import logging
import math
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from enum import Enum
from typing import List, Union, Sequence, Tuple, Callable, Optional

import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
from torch import cuda as torchcuda

from ..util.dtype import toFloatArray
from ..vector_model import VectorRegressionModel, VectorClassificationModel
from ..util.string import objectRepr
from .torch_data import TensorScaler, DataUtil, VectorDataUtil, ClassificationVectorDataUtil, TorchDataSet, \
    TorchDataSetProviderFromDataUtil, TorchDataSetProvider

_log = logging.getLogger(__name__)


class WrappedTorchModule(ABC):
    _log = _log.getChild(__qualname__)

    def __init__(self, cuda=True):
        self.cuda = cuda
        self.model = None
        self.outputScaler: Optional[TensorScaler] = None
        self.inputScaler: Optional[TensorScaler] = None

    def setTorchModel(self, model):
        self.model = model

    def getModelBytes(self):
        bytesIO = io.BytesIO()
        torch.save(self.model, bytesIO)
        return bytesIO.getvalue()

    def setModelBytes(self, modelBytes):
        modelFile = io.BytesIO(modelBytes)
        self._loadModel(modelFile)

    def getTorchModel(self):
        return self.model

    def _setCudaEnabled(self, isCudaEnabled):
        self.cuda = isCudaEnabled

    def _isCudaEnabled(self):
        return self.cuda

    def _loadModel(self, modelFile):
        try:
            self.model = torch.load(modelFile)
        except:
            if self._isCudaEnabled():
                self._log.warning("Loading of CUDA model failed, trying without CUDA...")
                if type(modelFile) != str:
                    modelFile.seek(0)
                self.model = torch.load(modelFile, map_location='cpu')
                self._setCudaEnabled(False)
                self._log.info("Model successfully loaded to CPU")
            else:
                raise

    @abstractmethod
    def createTorchModule(self):
        pass

    def __getstate__(self):
        state = dict(self.__dict__)
        del state["model"]
        state["modelBytes"] = self.getModelBytes()
        return state

    def __setstate__(self, d):
        modelBytes = None
        if "modelBytes" in d:
            modelBytes = d["modelBytes"]
            del d["modelBytes"]
        self.__dict__ = d
        if modelBytes is not None:
            self.setModelBytes(modelBytes)

    def apply(self, X: Union[torch.Tensor, np.ndarray, TorchDataSet], asNumpy=True, createBatch=False, mcDropoutSamples=None, mcDropoutProbability=None, scaleOutput=False,
            scaleInput=False) -> Union[torch.Tensor, np.ndarray, Tuple]:
        """
        Applies the model to the given input tensor and returns the result (normalized)

        :param X: the input tensor (either a batch or, if createBatch=True, a single data point) or data set.
            If it is a data set, a single tensor will be extracted from it, so the data set must not be too large to be processed at once.
        :param asNumpy: flag indicating whether to convert the result to a numpy.array (if False, return tensor)
        :param createBatch: whether to add an additional tensor dimension for a batch containing just one data point
        :param mcDropoutSamples: if not None, apply MC-Dropout-based inference with the respective number of samples; if None, apply regular inference
        :param mcDropoutProbability: the probability with which to apply dropouts in MC-Dropout-based inference; if None, use model's default
        :param scaleOutput: whether to scale the output that is produced by the underlying model (using this instance's output scaler)
        :param scaleInput: whether to scale the input (using this instance's input scaler) before applying the underlying model

        :return: an output tensor or, if MC-Dropout is applied, a pair (y, sd) where y the mean output tensor and sd is a tensor of the same dimension
            containing standard deviations
        """
        def extract(z):
            if scaleOutput:
                z = self.scaledOutput(z)
            if self._isCudaEnabled():
                z = z.cpu()
            z = z.detach()
            if asNumpy:
                z = z.numpy()
            return z

        model = self.getTorchModel()
        model.eval()

        if isinstance(X, TorchDataSet):
            X = next(X.iterBatches(X.size(), inputOnly=True, shuffle=False))
        elif isinstance(X, np.ndarray):
            X = toFloatArray(X)
            X = torch.from_numpy(X).float()

        if self._isCudaEnabled():
            X = X.cuda()
        if scaleInput:
            X = self.inputScaler.normalise(X)
        if createBatch:
            X = X.view(1, *X.size())

        maxValue = X.max().item()
        if maxValue > 2:
            _log.warning("Received input which is likely to not be correctly normalised: maximum value in input tensor is %f" % maxValue)

        if mcDropoutSamples is None:
            y = model(X)
            return extract(y)
        else:
            y, stddev = model.inferMCDropout(X, mcDropoutSamples, p=mcDropoutProbability)
            return extract(y), extract(stddev)

    def applyScaled(self, X: Union[torch.Tensor, np.ndarray, TorchDataSet], **kwargs) -> Union[torch.Tensor, np.ndarray]:
        """
        applies the model to the given input tensor and returns the scaled result (i.e. in the original scale)

        :param X: the input tensor or data set
        :param kwargs: parameters to pass on to apply

        :return: a scaled output tensor or, if MC-Dropout is applied, a pair (y, sd) of scaled tensors, where
            y the mean output tensor and sd is a tensor of the same dimension containing standard deviations
        """
        return self.apply(X, scaleOutput=True, scaleInput=True, **kwargs)

    def scaledOutput(self, output):
        return self.outputScaler.denormalise(output)

    def _extractParamsFromData(self, data: TorchDataSetProvider):
        self.outputScaler = data.getOutputTensorScaler()
        self.inputScaler = data.getInputTensorScaler()

    def fit(self, data: TorchDataSetProvider, **nnOptimiserParams):
        self._extractParamsFromData(data)
        optimiser = NNOptimiser(cuda=self.cuda, **nnOptimiserParams)
        optimiser.fit(self, data)


class _Optimiser(object):
    """
    Wrapper for classes inherited from torch.optim.Optimizer
    """
    def _makeOptimizer(self):
        optimiserArgs = dict(self.optimiserArgs)
        optimiserArgs.update({'lr': self.lr})
        if self.method == 'sgd':
            self.optimizer = optim.SGD(self.params, **optimiserArgs)
        elif self.method == 'asgd':
            self.optimizer = optim.ASGD(self.params, **optimiserArgs)
        elif self.method == 'adagrad':
            self.optimizer = optim.Adagrad(self.params, **optimiserArgs)
        elif self.method == 'adadelta':
            self.optimizer = optim.Adadelta(self.params, **optimiserArgs)
        elif self.method == 'adam':
            self.optimizer = optim.Adam(self.params, **optimiserArgs)
        elif self.method == 'adamw':
            self.optimizer = optim.AdamW(self.params, **optimiserArgs)
        elif self.method == 'adamax':
            self.optimizer = optim.Adamax(self.params, **optimiserArgs)
        elif self.method == 'rmsprop':
            self.optimizer = optim.RMSprop(self.params, **optimiserArgs)
        elif self.method == 'rprop':
            self.optimizer = optim.Rprop(self.params, **optimiserArgs)
        elif self.method == 'lbfgs':
            self.use_shrinkage = False
            self.optimizer = optim.LBFGS(self.params, **optimiserArgs)
        else:
            raise RuntimeError("Invalid optim method: " + self.method)

    def __init__(self, params, method, lr, max_grad_norm, lr_decay=1, start_decay_at=None, **optimiserArgs):
        """

        :param params: an iterable of torch.Tensor s or dict s. Specifies what Tensors should be optimized.
        :param method: string identifier for optimiser method to use
        :param lr: learnig rate
        :param max_grad_norm: max value for gradient shrinkage
        :param lr_decay: deacay rate
        :param start_decay_at: epoch to start learning rate decay
        :param optimiserArgs: keyword arguments to be used in actual torch optimiser
        """
        self.params = list(params)  # careful: params may be a generator
        self.last_ppl = None
        self.lr = lr
        self.max_grad_norm = max_grad_norm
        self.method = method
        self.lr_decay = lr_decay
        self.start_decay_at = start_decay_at
        self.start_decay = False
        self.optimiserArgs = optimiserArgs
        self.use_shrinkage = True
        self._makeOptimizer()

    def step(self, lossBackward: Callable):
        """

        :param lossBackward: callable, performs backward step and returns loss
        :return:
        """
        if self.use_shrinkage:
            def closureWithShrinkage():
                loss = lossBackward()

                # Compute gradients norm.
                grad_norm = 0
                for param in self.params:
                    grad_norm += math.pow(param.grad.data.norm(), 2)

                grad_norm = math.sqrt(grad_norm)
                if grad_norm > 0:
                    shrinkage = self.max_grad_norm / grad_norm
                else:
                    shrinkage = 1.

                for param in self.params:
                    if shrinkage < 1:
                        param.grad.data.mul_(shrinkage)

                return loss

            closure = closureWithShrinkage
        else:
            closure = lossBackward

        loss = self.optimizer.step(closure)
        return loss

    # decay learning rate if val perf does not improve or we hit the start_decay_at limit
    def updateLearningRate(self, ppl, epoch):
        if self.start_decay_at is not None and epoch >= self.start_decay_at:
            self.start_decay = True
        if self.last_ppl is not None and ppl > self.last_ppl:
            self.start_decay = True

        if self.start_decay:
            self.lr = self.lr * self.lr_decay
            print("Decaying learning rate to %g" % self.lr)
        #only decay for one epoch
        self.start_decay = False

        self.last_ppl = ppl

        self._makeOptimizer()


class NNLossEvaluator(ABC):
    """
    Provides functionality for a training process.
    An instance cannot be used for more than one simultaneous training process.
    """

    @abstractmethod
    def getTrainingCriterion(self):
        """Gets the optimisation criterion (loss function) for training"""
        pass

    @abstractmethod
    def startTraining(self, cuda):
        """Prepares for a new training process, initialising internal state as required"""
        pass

    @abstractmethod
    def startValidationCollection(self, groundTruthShape):
        """
        Initiates validation data collection for a new epoch

        :param groundTruthShape: the tensor shape of a single ground truth data point
        """
        pass

    @abstractmethod
    def collectValidationResultBatch(self, output, groundTruth):
        """
        Collects, for validation, the given output and ground truth data (tensors holding data on one batch,
        where the first dimensions is the batch)

        :param output: the model's output
        :param groundTruth: the corresponding ground truth
        :return:
        """
        pass

    @abstractmethod
    def endValidationCollection(self) -> OrderedDict:
        """
        Computes validation metrics based on the data previously collected.

        :return: an ordered dictionary with validation metrics
        """
        pass

    def getValidationMetricName(self) -> str:
        """
        Gets the name of the metric (key of dictionary as returned by endValidationCollection), which
        is defining for the quality of the model

        :return: the name of the metrics that is indicated of model quality
        """
        pass


class NNLossEvaluatorRegression(NNLossEvaluator):
    """A loss evaluator for (multi-variate) regression"""

    class LossFunction(Enum):
        L1LOSS = "L1Loss"
        L2LOSS = "L2Loss"
        MSELOSS = "MSELoss"
        SMOOTHL1LOSS = "SmoothL1Loss"

    def __init__(self, lossFn: LossFunction):
        if lossFn is None:
            lossFn = self.LossFunction.L2LOSS
        try:
            self.lossFn = self.LossFunction(lossFn)
        except ValueError:
            raise Exception(f"Loss function {lossFn} not supported. Available are: {[e.value for e in self.LossFunction]}")

        # transient members: state for validation
        self.total_loss_l1 = None
        self.total_loss_l2 = None
        self.outputDims = None
        self.allTrueOutputs = None

    def __str__(self):
        return f"{self.__class__.__name__}[{self.lossFn}]"

    def startTraining(self, cuda):
        self.evaluateL1 = nn.L1Loss(reduction='sum')
        self.evaluateL2 = nn.MSELoss(reduction='sum')
        if cuda:
            self.evaluateL1 = self.evaluateL1.cuda()
            self.evaluateL2 = self.evaluateL2.cuda()

    def getTrainingCriterion(self):
        if self.lossFn is self.LossFunction.L1LOSS:
            criterion = nn.L1Loss(reduction='sum')
        elif self.lossFn is self.LossFunction.L2LOSS or self.lossFn == self.LossFunction.MSELOSS:
            criterion = nn.MSELoss(reduction='sum')
        elif self.lossFn is self.LossFunction.SMOOTHL1LOSS:
            criterion = nn.SmoothL1Loss(reduction='sum')
        else:
            raise AssertionError(f"Loss function {self.lossFn} defined but instantiation not implemented.")
        return criterion

    def startValidationCollection(self, groundTruthShape):
        if len(groundTruthShape) != 1:
            raise ValueError("Outputs that are not vectors are currently unsupported")
        self.outputDims = groundTruthShape[-1]
        self.total_loss_l1 = np.zeros(self.outputDims)
        self.total_loss_l2 = np.zeros(self.outputDims)
        self.allTrueOutputs = None

    def collectValidationResultBatch(self, output, groundTruth):
        # obtain series of outputs per output dimension: (batch_size, output_size) -> (output_size, batch_size)
        predictedOutput = output.permute(1, 0)
        trueOutput = groundTruth.permute(1, 0)

        if self.allTrueOutputs is None:
            self.allTrueOutputs = trueOutput
        else:
            self.allTrueOutputs = torch.cat((self.allTrueOutputs, trueOutput), dim=1)

        for i in range(self.outputDims):
            self.total_loss_l1[i] += self.evaluateL1(predictedOutput[i], trueOutput[i]).item()
            self.total_loss_l2[i] += self.evaluateL2(predictedOutput[i], trueOutput[i]).item()

    def endValidationCollection(self):
        outputDims = self.outputDims
        rae = np.zeros(outputDims)
        rrse = np.zeros(outputDims)
        mae = np.zeros(outputDims)
        mse = np.zeros(outputDims)

        for i in range(outputDims):
            mean = torch.mean(self.allTrueOutputs[i])
            refModelErrors = self.allTrueOutputs[i] - mean
            refModelSumAbsErrors = torch.sum(torch.abs(refModelErrors)).item()
            refModelSumSquaredErrors = torch.sum(refModelErrors * refModelErrors).item()
            numSamples = refModelErrors.size(0)

            mae[i] = self.total_loss_l1[i] / numSamples
            mse[i] = self.total_loss_l2[i] / numSamples
            rae[i] = self.total_loss_l1[i] / refModelSumAbsErrors if refModelSumAbsErrors != 0 else np.inf
            rrse[i] = np.sqrt(mse[i]) / np.sqrt(
                refModelSumSquaredErrors / numSamples) if refModelSumSquaredErrors != 0 else np.inf

        metrics = OrderedDict([("RRSE", np.mean(rrse)), ("RAE", np.mean(rae)), ("MSE", np.mean(mse)), ("MAE", np.mean(mae))])
        return metrics

    def getValidationMetricName(self):
        if self.lossFn is self.LossFunction.L1LOSS or self.lossFn is self.LossFunction.SMOOTHL1LOSS:
            return "MAE"
        elif self.lossFn is self.LossFunction.L2LOSS or self.lossFn is self.LossFunction.MSELOSS:
            return "MSE"
        else:
            raise AssertionError(f"No selection criterion defined for loss function {self.lossFn}")


class NNLossEvaluatorClassification(NNLossEvaluator):
    """A loss evaluator for (multi-variate) regression"""

    class LossFunction(Enum):
        CROSSENTROPY = "CrossEntropy"

    def __init__(self, lossFn: LossFunction):
        if lossFn is None:
            lossFn = self.LossFunction.CROSSENTROPY
        try:
            self.lossFn = self.LossFunction(lossFn)
        except ValueError:
            raise Exception(f"Loss function {lossFn} not supported. Available are: {[e.value for e in self.LossFunction]}")

        # transient members: state for validation
        self.totalLossCE = None
        self.numValidationSamples = None

    def __str__(self):
        return f"{self.__class__.__name__}[{self.lossFn}]"

    def startTraining(self, cuda):
        self.evaluateCE = nn.CrossEntropyLoss(reduction="sum")
        if cuda:
            self.evaluateCE = self.evaluateCE.cuda()

    def getTrainingCriterion(self):
        if self.lossFn is self.LossFunction.CROSSENTROPY:
            criterion = nn.CrossEntropyLoss(reduction='sum')
        else:
            raise AssertionError(f"Loss function {self.lossFn} defined but instantiation not implemented.")
        return criterion

    def startValidationCollection(self, groundTruthShape):
        if len(groundTruthShape) != 0:
            raise ValueError("Outputs must be scalars, specifically integers indicating the true class indices, not tensors")
        self.totalLossCE = 0
        self.numValidationSamples = 0

    def collectValidationResultBatch(self, output, groundTruth):
        self.totalLossCE += self.evaluateCE(output, groundTruth).item()
        self.numValidationSamples += output.shape[0]

    def endValidationCollection(self):
        ce = self.totalLossCE / self.numValidationSamples
        metrics = OrderedDict([("CE", ce), ("GeoMeanProbTrueClass", math.exp(-ce))])
        return metrics

    def getValidationMetricName(self):
        if self.lossFn is self.LossFunction.CROSSENTROPY:
            return "CE"
        else:
            raise AssertionError(f"No selection criterion defined for loss function {self.lossFn}")


class NNOptimiser:
    _log = _log.getChild(__qualname__)

    def __init__(self, lossEvaluator: NNLossEvaluator = None, cuda=True, gpu=None, optimiser="adam", optimiserClip=10., optimiserLR=0.001,
             batchSize=None, epochs=1000, trainFraction=0.75, scaledOutputs=False, optimiserLRDecay=1, startLRDecayAtEpoch=None, **optimiserArgs):
        """
        :param cuda: whether to use CUDA or not
        :param lossEvaluator: the loss evaluator to use
        :param gpu: index of the gpu to be used, if parameter cuda is True
        :param optimiser: the optimizer to be used; defaults to "adam"
        :param optimiserClip: max value for gradient clipping
        :param optimiserLR: the optimizer's learning rate decay
        :param trainFraction: the fraction of the data used for training; defaults to 0.75
        :param scaledOutputs: whether to scale all outputs, resulting in computations of the loss function based on scaled values rather than normalised values.
            Enabling scaling may not be appropriate in cases where there are multiple outputs on different scales/with completely different units.
        :param optimiserArgs: keyword arguments to be used in actual torch optimiser
        """
        if optimiser == 'lbfgs':
            largeBatchSize = 1e12
            if batchSize is not None:
                _log.warning(f"LBFGS does not make use of batches, therefore using largeBatchSize {largeBatchSize} to achieve use of a single batch")
            batchSize = largeBatchSize
        else:
            if batchSize is None:
                batchSize = 64

        if lossEvaluator is None:
            raise ValueError("Must provide a loss evaluator")

        self.epochs = epochs
        self.batchSize = batchSize
        self.optimiserLR = optimiserLR
        self.optimiserClip = optimiserClip
        self.optimiser = optimiser
        self.cuda = cuda
        self.gpu = gpu
        self.trainFraction = trainFraction
        self.scaledOutputs = scaledOutputs
        self.lossEvaluator = lossEvaluator
        self.startLRDecayAtEpoch = startLRDecayAtEpoch
        self.optimiserLRDecay = optimiserLRDecay
        self.optimiserArgs = optimiserArgs

        self.trainingLog = None
        self.bestEpoch = None

    def __str__(self):
        return f"{self.__class__.__name__}[cuda={self.cuda}, optimiser={self.optimiser}, lossEvaluator={self.lossEvaluator}, epochs={self.epochs}, batchSize={self.batchSize}, LR={self.optimiserLR}, clip={self.optimiserClip}, gpu={self.gpu}]"

    def fit(self, model: WrappedTorchModule, data: Union[DataUtil, List[DataUtil], TorchDataSetProvider]):
        self._log.info(f"Learning parameters of {model} via {self}")

        def toDataSetProvider(d) -> TorchDataSetProvider:
            if isinstance(d, TorchDataSetProvider):
                return d
            elif isinstance(d, DataUtil):
                return TorchDataSetProviderFromDataUtil(d, self.cuda)

        if type(data) != list:
            dataSetProviders = [toDataSetProvider(data)]
        else:
            dataSetProviders = [toDataSetProvider(item) for item in data]

        # initialise data to be generated
        self.trainingLog = []
        self.bestEpoch = None

        def trainingLog(s):
            self._log.info(s)
            self.trainingLog.append(s)

        self._init_cuda()

        # Set the random seed manually for reproducibility.
        seed = 42
        torch.manual_seed(seed)
        if self.cuda:
            torchcuda.manual_seed_all(seed)
        torch.backends.cudnn.benchmark = False
        torch.backends.cudnn.deterministic = True

        # obtain data, splitting it into training and validation set(s)
        validationSets = []
        trainingSets = []
        outputScalers = []
        _log.info("Obtaining input/output training instances")
        for idxDataSetProvider, dataSetProvider in enumerate(dataSetProviders):
            outputScalers.append(dataSetProvider.getOutputTensorScaler())
            trainS, valS = dataSetProvider.provideSplit(self.trainFraction)
            trainingLog(f"Data set {idxDataSetProvider+1}/{len(dataSetProviders)}: #train={trainS.size()}, #validation={valS.size()}")
            validationSets.append(valS)
            trainingSets.append(trainS)
        trainingLog("Number of validation sets: %d" % len(validationSets))

        torchModel = model.createTorchModule()
        if self.cuda:
            torchModel.cuda()
        model.setTorchModel(torchModel)

        nParams = sum([p.nelement() for p in torchModel.parameters()])
        trainingLog('Number of parameters: %d' % nParams)

        criterion = self.lossEvaluator.getTrainingCriterion()

        if self.cuda:
            criterion = criterion.cuda()

        best_val = 1e9
        best_epoch = 0
        optim = _Optimiser(torchModel.parameters(), method=self.optimiser, lr=self.optimiserLR,
            max_grad_norm=self.optimiserClip, lr_decay=self.optimiserLRDecay, start_decay_at=self.startLRDecayAtEpoch,
            **self.optimiserArgs)

        bestModelBytes = None
        self.lossEvaluator.startTraining(self.cuda)
        validationMetricName = self.lossEvaluator.getValidationMetricName()
        try:
            self._log.info('Begin training')
            self._log.info('Press Ctrl+C to end training early')
            for epoch in range(1, self.epochs + 1):
                epoch_start_time = time.time()

                # perform training step, processing all the training data once
                train_loss = self._train(trainingSets, torchModel, criterion, optim, self.batchSize, self.cuda, outputScalers)

                # perform validation, computing the mean metrics across all validation sets (if more than one)
                metricsSum = None
                metricsKeys = None
                for i, (validationSet, outputScaler) in enumerate(zip(validationSets, outputScalers)):
                    metrics = self._evaluate(validationSet, torchModel, outputScaler)
                    metricsArray = np.array(list(metrics.values()))
                    if i == 0:
                        metricsSum = metricsArray
                        metricsKeys = metrics.keys()
                    else:
                        metricsSum += metricsArray
                metricsSum /= len(validationSets)  # mean results
                metrics = dict(zip(metricsKeys, metricsSum))

                # check for new best result according to validation results
                current_val = metrics[self.lossEvaluator.getValidationMetricName()]
                isNewBest = current_val < best_val
                if isNewBest:
                    best_val = current_val
                    best_epoch = epoch
                    bestStr = "best {:s} {:5.6f} from this epoch".format(validationMetricName, best_val)
                else:
                    bestStr = "best {:s} {:5.6f} from epoch {:d}".format(validationMetricName, best_val, best_epoch)
                trainingLog(
                    'Epoch {:3d}/{} completed in {:5.2f}s | train loss {:5.4f} | validation {:s} | {:s}'.format(
                        epoch, self.epochs, (time.time() - epoch_start_time), train_loss,
                        ", ".join(["%s %5.4f" % e for e in metrics.items()]),
                        bestStr))
                if isNewBest:
                    bestModelBytes = model.getModelBytes()
            trainingLog("Training complete")
        except KeyboardInterrupt:
            trainingLog('Exiting from training early')
        trainingLog('Best model is from epoch %d with %s %f on validation set' % (best_epoch, validationMetricName, best_val))
        self.bestEpoch = best_epoch

        # reload best model
        model.setModelBytes(bestModelBytes)

    def getTrainingLog(self):
        return self.trainingLog

    def getBestEpoch(self):
        return self.bestEpoch

    def _applyModel(self, model, X, groundTruth, outputScaler: TensorScaler):
        output = model(X)
        if self.scaledOutputs:
            output, groundTruth = self._scaledValues(output, groundTruth, outputScaler)
        return output, groundTruth

    @classmethod
    def _scaledValues(cls, modelOutput, groundTruth, outputScaler):
        scaledOutput = outputScaler.denormalise(modelOutput)
        scaledTruth = outputScaler.denormalise(groundTruth)
        return scaledOutput, scaledTruth

    def _train(self, dataSets: Sequence[TorchDataSet], model: nn.Module, criterion: nn.modules.loss._Loss,
            optim: _Optimiser, batch_size: int, cuda: bool, outputScalers: Sequence[TensorScaler]):
        """Performs one training epoch"""
        model.train()
        total_loss = 0
        n_samples = 0
        numOutputsPerDataPoint = None
        for dataSet, outputScaler in zip(dataSets, outputScalers):
            for X, Y in dataSet.iterBatches(batch_size, shuffle=True):
                if numOutputsPerDataPoint is None:
                    outputShape = Y.shape[1:]
                    numOutputsPerDataPoint = functools.reduce(lambda x, y: x * y, outputShape, 1)

                def closure():
                    model.zero_grad()
                    output, groundTruth = self._applyModel(model, X, Y, outputScaler)
                    loss = criterion(output, groundTruth)
                    loss.backward()
                    return loss

                loss = optim.step(closure)
                total_loss += loss.item()
                numDataPointsInBatch = Y.size(0)
                n_samples += numDataPointsInBatch * numOutputsPerDataPoint
        return total_loss / n_samples

    def _evaluate(self, dataSet: TorchDataSet, model: nn.Module, outputScaler: TensorScaler):
        """Evaluates the model on the given data set (a validation set)"""
        model.eval()

        groundTruthShape = None
        for X, Y in dataSet.iterBatches(self.batchSize, shuffle=False):
            if groundTruthShape is None:
                groundTruthShape = Y.shape[1:]  # the shape of the output of a single model application
                self.lossEvaluator.startValidationCollection(groundTruthShape)
            with torch.no_grad():
                output, groundTruth = self._applyModel(model, X, Y, outputScaler)
            self.lossEvaluator.collectValidationResultBatch(output, groundTruth)

        return self.lossEvaluator.endValidationCollection()

    def _init_cuda(self):
        """Initialises CUDA (for learning) by setting the appropriate device if necessary"""
        if self.cuda:
            deviceCount = torchcuda.device_count()
            if deviceCount == 0:
                raise Exception("CUDA is enabled but no device found")
            if self.gpu is None:
                if deviceCount > 1:
                    _log.warning("More than one GPU detected. Default GPU index is set to 0.")
                gpuIndex = 0
            else:
                gpuIndex = self.gpu
            torchcuda.set_device(gpuIndex)
        elif torchcuda.is_available():
            self._log.warning("You have a CUDA device, so you should probably run with cuda=True")


class WrappedTorchVectorModule(WrappedTorchModule, ABC):
    """
    Base class for wrapped torch modules that map vectors to vectors
    """
    def __init__(self, cuda: bool = True):
        super().__init__(cuda=cuda)
        self.inputDim = None
        self.outputDim = None

    def _extractParamsFromData(self, data: TorchDataSetProvider):
        super()._extractParamsFromData(data)
        self.inputDim = data.getInputDim()
        self.outputDim = data.getModelOutputDim()

    def createTorchModule(self):
        return self.createTorchVectorModule(self.inputDim, self.outputDim)

    @abstractmethod
    def createTorchVectorModule(self, inputDim, outputDim):
        pass


class TorchVectorRegressionModel(VectorRegressionModel):
    def __init__(self, modelClass: Callable[..., WrappedTorchVectorModule], modelArgs, modelKwArgs, normalisationMode, nnOptimiserParams):
        """
        :param modelClass:
        :param modelArgs:
        :param modelKwArgs:
        :param normalisationMode:
        :param nnOptimiserParams:
        """
        super().__init__()
        if "lossEvaluator" not in nnOptimiserParams:
            nnOptimiserParams["lossEvaluator"] = NNLossEvaluatorRegression(NNLossEvaluatorRegression.LossFunction.MSELOSS)
        self.normalisationMode = normalisationMode
        self.nnOptimiserParams = nnOptimiserParams
        self.modelClass = modelClass
        self.modelArgs = modelArgs
        self.modelKwArgs = modelKwArgs
        self.model: Optional[WrappedTorchVectorModule] = None

    def createTorchVectorModel(self) -> WrappedTorchVectorModule:
        return self.modelClass(*self.modelArgs, **self.modelKwArgs)

    def _fit(self, inputs: pd.DataFrame, outputs: pd.DataFrame):
        self.model = self.createTorchVectorModel()
        dataUtil = VectorDataUtil(inputs, outputs, self.model.cuda, normalisationMode=self.normalisationMode)
        dataSetProvider = TorchDataSetProviderFromDataUtil(dataUtil, self.model.cuda)
        self.model.fit(dataSetProvider, **self.nnOptimiserParams)

    def _predict(self, inputs: pd.DataFrame) -> pd.DataFrame:
        yArray = self.model.applyScaled(inputs.values)
        return pd.DataFrame(yArray, columns=self.getModelOutputVariableNames())

    def __str__(self):
        return objectRepr(self, ["model", "normalisationMode", "nnOptimiserParams"])


class TorchVectorClassificationModel(VectorClassificationModel):
    def __init__(self, modelClass: Callable[..., WrappedTorchVectorModule], modelArgs, modelKwArgs, normalisationMode, nnOptimiserParams):
        """
        :param modelClass:
        :param modelArgs:
        :param modelKwArgs:
        :param normalisationMode:
        :param nnOptimiserParams:
        """
        super().__init__()
        if "lossEvaluator" not in nnOptimiserParams:
            nnOptimiserParams["lossEvaluator"] = NNLossEvaluatorClassification(NNLossEvaluatorClassification.LossFunction.CROSSENTROPY)
        self.normalisationMode = normalisationMode
        self.nnOptimiserParams = nnOptimiserParams
        self.modelClass = modelClass
        self.modelArgs = modelArgs
        self.modelKwArgs = modelKwArgs
        self.model: Optional[WrappedTorchVectorModule] = None

    def createTorchVectorModel(self) -> WrappedTorchVectorModule:
        return self.modelClass(*self.modelArgs, **self.modelKwArgs)

    def _createDataSetProvider(self, inputs: pd.DataFrame, outputs: pd.DataFrame) -> TorchDataSetProvider:
        dataUtil = ClassificationVectorDataUtil(inputs, outputs, self.model.cuda, len(self._labels),
            normalisationMode=self.normalisationMode)
        return TorchDataSetProviderFromDataUtil(dataUtil, self.model.cuda)

    def _fitClassifier(self, inputs: pd.DataFrame, outputs: pd.DataFrame):
        if len(outputs.columns) != 1:
            raise ValueError("Expected one output dimension: the class labels")

        # transform outputs: for each data point, the new output shall be the index in the list of labels
        labels: pd.Series = outputs.iloc[:, 0]
        outputs = pd.DataFrame([self._labels.index(l) for l in labels], columns=outputs.columns, index=outputs.index)

        self.model = self.createTorchVectorModel()

        dataSet = self._createDataSetProvider(inputs, outputs)
        self.model.fit(dataSet, **self.nnOptimiserParams)

    def _predict(self, inputs: pd.DataFrame) -> pd.DataFrame:
        return self.convertClassProbabilitiesToPredictions(self._predictClassProbabilities(inputs))

    def _predictOutputsForInputDataFrame(self, inputs: pd.DataFrame) -> np.ndarray:
        return self.model.applyScaled(inputs.values, asNumpy=True)

    def _predictClassProbabilities(self, inputs: pd.DataFrame):
        y = self._predictOutputsForInputDataFrame(inputs)
        normalisationConstants = y.sum(axis=1)
        for i in range(y.shape[0]):
            y[i,:] /= normalisationConstants[i]
        return pd.DataFrame(y, columns=self._labels)

    def __str__(self):
        return objectRepr(self, ["model", "normalisationMode", "nnOptimiserParams"])