from megatron.core import parallel_state
import torch 

from nemo_aligner.utils.distributed import broadcast_2d_tensor
from nemo.collections.nlp.modules.common.text_generation_utils import get_model_parallel_src_rank
from typing import List



class GPTGenerateTRTLLM():
    def __init__(self, cfg, tokenizer, trt_model_dir="/tmp/trt_llm_model", ):
        self.cfg = cfg
        self.tokenizer = tokenizer
        self.max_generation_length = self.cfg.ppo.length_params.get('max_length')
        self.max_context_length = 2048
        self.max_attn_window_size = 4096
        self.generation_batch_size = self.cfg.ppo.get('rollout_micro_batch_size')
        self._trtllm_model_compiled = False

        self._import_tensorrt_llm()
        self.trt_llm_exporter = TRTExport(trt_model_dir, load_model=False)
        self.stop_words = self._create_stop_words()

        self.sampling_config = tensorrt_llm.runtime.SamplingConfig(
            end_id=tokenizer.eos_id, 
            pad_id=tokenizer.eos_id, #TODO
            temperature=self.cfg.ppo.sampling_params.get('temperature'),
            top_k=self.cfg.ppo.sampling_params.get('top_k'),
            top_p=self.cfg.ppo.sampling_params.get('top_p'),
            max_new_tokens=self.max_generation_length,
            max_attention_window_size=self.max_attn_window_size,
            stop_words_list=self.stop_words
        )

    def _import_tensorrt_llm(self):        
        from mpi4py import MPI 
        from nemo.export import TensorRTLLM as TRTExport
        from nemo.export.trt_llm.tensorrt_llm_run import forward as trtllm_forward
        import tensorrt_llm

        globals()["TRTExport"] = TRTExport
        globals()["tensorrt_llm"] = tensorrt_llm
        globals()["trtllm_forward"] = trtllm_forward
        
    def _create_stop_words(self):
        # stop_id = self.tokenizer.text_to_ids("<extra_id_1>")
        stop_id = [29966, 17833, 29918, 333, 29918, 29896, 29958]
        eos_id = self.tokenizer.eos_id
        stop_strings = [stop_id]
        stop_tokens = [[eos_id]]

        stop_words = [[],[]]
        for w in (stop_strings+stop_tokens):
            stop_words[0] += w
            stop_words[1].append(len(stop_words[0]))
        stop_words[1] += [-1] * (len(stop_words[0]) - len(stop_words[1]))

        stop_words = torch.IntTensor(stop_words).cuda()
        return stop_words.unsqueeze(0).repeat(self.generation_batch_size,1,1)

    def refit(self, model):
        if not self._trtllm_model_compiled:
            self.trt_llm_exporter.build(
                nemo_model = model, 
                nemo_model_config = self.cfg, 
                tokenizer = self.tokenizer,
                max_input_len=self.max_context_length,
                max_output_len=self.max_generation_length,
                max_batch_size=self.cfg.ppo.get('rollout_micro_batch_size'),
                use_refit=True)
            self._trtllm_model_compiled = True
        else:
            self.trt_llm_exporter.refit(
                nemo_model = model, 
                nemo_model_config = self.cfg, 
            )

    def generate(self, inputs):
        prompt_tokens, prompt_lengths = inputs

        batch_input_ids = []
        for idx in range(prompt_tokens.shape[0]):
            batch_input_ids.append(prompt_tokens[idx][0:prompt_lengths[idx]].cpu())

        output_ids = self.trt_llm_exporter.model_runner.generate(
            batch_input_ids=batch_input_ids,
            sampling_config=self.sampling_config,
            streaming=False)

        # output_ids shape [mbs, beam dim, sequence len]
        # remove beam dim
        mbs = output_ids.shape[0]
        if mbs == 1:
            output_ids = output_ids.view([1,output_ids.shape[-1]])
        else:
            output_ids = output_ids.squeeze()
        output_ids = output_ids.to(torch.int64)

        output_ids = torch.Tensor.tolist(output_ids)
        sentences = [self.tokenizer.ids_to_text(output) for output in output_ids]
        output = {
            "token_ids" : output_ids,
            "sentences" : sentences,
        }
        
        return output


    def free(self):        
        self.trt_llm_exporter.unload_engine(keep_generate_session=True)
        torch.cuda.empty_cache()
