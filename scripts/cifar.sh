#########
# cifar #
#########
device_idx=1
dataset_name="cifar"
model_name="ctmc"
seed=42
src_nfe=1024
num_samples=64
for scheduler_name in "euler" ; do # "euler" "tweedie" "gillespie" "pc" 
python3 main.py \
    --pretrained_model_path "weights/$dataset_name-$model_name"   \
    --dataset_name $dataset_name        \
    --model_name $model_name            \
    --scheduler_name $scheduler_name    \
    --noise_schedule "loglinear"        \
    --num_samples $num_samples          \
    --batch_size $num_samples           \
    --gibbs_iter 0                      \
    --src_num_function_eval $src_nfe    \
    --tgt_num_function_eval 256         \
    --device "cuda:$device_idx"

for nfe in 16 32 64 128 256 ; do
python3 eval.py \
    --pretrained_model_path "weights/$dataset_name-$model_name"   \
    --sampling_schedule_name "uniform"  \
    --dataset_name $dataset_name        \
    --model_name $model_name            \
    --scheduler_name $scheduler_name    \
    --num_samples 16384                 \
    --batch_size 512                    \
    --src_nfe $src_nfe                  \
    --tgt_nfe $nfe                      \
    --seed $seed                        \
    --output_dir "runs-eval"            \
    --save_dir "runs-gen_x0"            \
    --device "cuda:$device_idx"

python3 eval.py \
    --pretrained_model_path "weights/$dataset_name-$model_name"   \
    --sampling_schedule_path "runs/$scheduler_name/$dataset_name-$model_name/sampling_schedule_list-nfe_$src_nfe-samples_$num_samples.pt"   \
    --sampling_schedule_name "jys"      \
    --dataset_name $dataset_name        \
    --model_name $model_name            \
    --scheduler_name $scheduler_name    \
    --num_samples 16384                 \
    --batch_size 512                    \
    --src_nfe $src_nfe                  \
    --tgt_nfe $nfe                      \
    --seed $seed                        \
    --output_dir "runs-eval"            \
    --save_dir "runs-gen_x0"            \
    --device "cuda:$device_idx"

done
done