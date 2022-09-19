#!/usr/bin/env python3
# Copyright (c) 2022 Lincoln D. Stein (https://github.com/lstein)

import os
import re
import sys
import copy
import warnings
import time
import ldm.dream.readline
from ldm.dream.args import Args, metadata_dumps
from ldm.dream.pngwriter import PngWriter
from ldm.dream.server import DreamServer, ThreadingDreamServer
from ldm.dream.image_util import make_grid
from omegaconf import OmegaConf

# Placeholder to be replaced with proper class that tracks the
# outputs and associates with the prompt that generated them.
# Just want to get the formatting look right for now.
output_cntr = 0

def main():
    """Initialize command-line parsers and the diffusion model"""
    opt  = Args()
    args = opt.parse_args()
    if not args:
        sys.exit(-1)

    if args.laion400m:
        print('--laion400m flag has been deprecated. Please use --model laion400m instead.')
        sys.exit(-1)
    if args.weights:
        print('--weights argument has been deprecated. Please edit ./configs/models.yaml, and select the weights using --model instead.')
        sys.exit(-1)

    print('* Initializing, be patient...\n')
    sys.path.append('.')
    from ldm.generate import Generate

    # these two lines prevent a horrible warning message from appearing
    # when the frozen CLIP tokenizer is imported
    import transformers
    transformers.logging.set_verbosity_error()

    # creating a simple Generate object with a handful of
    # defaults passed on the command line.
    # additional parameters will be added (or overriden) during
    # the user input loop
    try:
        gen = Generate(
            conf           = opt.conf,
            model          = opt.model,
            sampler_name   = opt.sampler_name,
            embedding_path = opt.embedding_path,
            full_precision = opt.full_precision,
        )
    except (FileNotFoundError, IOError, KeyError) as e:
        print(f'{e}. Aborting.')
        sys.exit(-1)

    # make sure the output directory exists
    if not os.path.exists(opt.outdir):
        os.makedirs(opt.outdir)

    # load the infile as a list of lines
    infile = None
    if opt.infile:
        try:
            if os.path.isfile(opt.infile):
                infile = open(opt.infile, 'r', encoding='utf-8')
            elif opt.infile == '-':  # stdin
                infile = sys.stdin
            else:
                raise FileNotFoundError(f'{opt.infile} not found.')
        except (FileNotFoundError, IOError) as e:
            print(f'{e}. Aborting.')
            sys.exit(-1)

    if opt.seamless:
        print(">> changed to seamless tiling mode")

    # preload the model
    gen.load_model()

    if not infile:
        print(
            "\n* Initialization done! Awaiting your command (-h for help, 'q' to quit)"
        )

    # web server loops forever
    if opt.web:
        dream_server_loop(gen, opt.host, opt.port, opt.outdir)
        sys.exit(0)

    main_loop(gen, opt, infile)

# TODO: main_loop() has gotten busy. Needs to be refactored.
def main_loop(gen, opt, infile):
    """prompt/read/execute loop"""
    done = False
    path_filter = re.compile(r'[<>:"/\\|?*]')
    last_results = list()
    model_config = OmegaConf.load(opt.conf)[opt.model]

    # os.pathconf is not available on Windows
    if hasattr(os, 'pathconf'):
        path_max = os.pathconf(opt.outdir, 'PC_PATH_MAX')
        name_max = os.pathconf(opt.outdir, 'PC_NAME_MAX')
    else:
        path_max = 260
        name_max = 255

    while not done:
        try:
            command = get_next_command(infile)
        except EOFError:
            done = True
            continue

        # skip empty lines
        if not command.strip():
            continue

        if command.startswith(('#', '//')):
            continue

        if len(command.strip()) == 1 and command.startswith('q'):
            done = True
            break

        if command.startswith(
            '!dream'
        ):   # in case a stored prompt still contains the !dream command
            command.replace('!dream','',1)

        if opt.parse_cmd(command) is None:
            continue
        if len(opt.prompt) == 0:
            print('\nTry again with a prompt!')
            continue

        # width and height are set by model if not specified
        if not opt.width:
            opt.width = model_config.width
        if not opt.height:
            opt.height = model_config.height
        
        # retrieve previous value!
        if opt.init_img is not None and re.match('^-\\d+$', opt.init_img):
            try:
                opt.init_img = last_results[int(opt.init_img)][0]
                print(f'>> Reusing previous image {opt.init_img}')
            except IndexError:
                print(
                    f'>> No previous initial image at position {opt.init_img} found')
                opt.init_img = None
                continue

        if opt.seed is not None and opt.seed < 0:   # retrieve previous value!
            try:
                opt.seed = last_results[opt.seed][1]
                print(f'>> Reusing previous seed {opt.seed}')
            except IndexError:
                print(f'>> No previous seed at position {opt.seed} found')
                opt.seed = None
                continue

        # TODO - move this into a module
        if opt.with_variations is not None:
            # shotgun parsing, woo
            parts = []
            broken = False  # python doesn't have labeled loops...
            for part in opt.with_variations.split(','):
                seed_and_weight = part.split(':')
                if len(seed_and_weight) != 2:
                    print(f'could not parse with_variation part "{part}"')
                    broken = True
                    break
                try:
                    seed = int(seed_and_weight[0])
                    weight = float(seed_and_weight[1])
                except ValueError:
                    print(f'could not parse with_variation part "{part}"')
                    broken = True
                    break
                parts.append([seed, weight])
            if broken:
                continue
            if len(parts) > 0:
                opt.with_variations = parts
            else:
                opt.with_variations = None

        if opt.prompt_as_dir:
            # sanitize the prompt to a valid folder name
            subdir = path_filter.sub('_', opt.prompt)[:name_max].rstrip(' .')

            # truncate path to maximum allowed length
            # 27 is the length of '######.##########.##.png', plus two separators and a NUL
            subdir = subdir[:(path_max - 27 - len(os.path.abspath(opt.outdir)))]
            current_outdir = os.path.join(opt.outdir, subdir)

            print('Writing files to directory: "' + current_outdir + '"')

            # make sure the output directory exists
            if not os.path.exists(current_outdir):
                os.makedirs(current_outdir)
        else:
            if not os.path.exists(opt.outdir):
                os.makedirs(opt.outdir)
            current_outdir = opt.outdir

        # Here is where the images are actually generated!
        last_results = []
        try:
            file_writer = PngWriter(current_outdir)
            prefix = file_writer.unique_prefix()
            results = []  # list of filename, prompt pairs
            grid_images      = dict()  # seed -> Image, only used if `opt.grid`
            prior_variations = opt.with_variations or []
            first_seed       = opt.seed

            def image_writer(image, seed, upscaled=False):
                path = None
                nonlocal first_seed
                nonlocal prior_variations
                if opt.grid:
                    grid_images[seed] = image
                else:
                    if upscaled and opt.save_original:
                        filename = f'{prefix}.{seed}.postprocessed.png'
                    else:
                        filename = f'{prefix}.{seed}.png'
                    if opt.variation_amount > 0:
                        first_seed             = first_seed or seed
                        this_variation         = [[seed, opt.variation_amount]]
                        opt.with_variations    = prior_variations + this_variation
                        formatted_dream_prompt = opt.dream_prompt_str(seed=first_seed)
                    elif len(prior_variations) > 0:
                        formatted_dream_prompt = opt.dream_prompt_str(seed=first_seed)
                    else:
                        formatted_dream_prompt = opt.dream_prompt_str(seed=seed)
                    path = file_writer.save_image_and_prompt_to_png(
                        image           = image,
                        dream_prompt    = formatted_dream_prompt,
                        metadata        = metadata_dumps(
                            opt,
                            seeds      = [seed],
                            model_hash = gen.model_hash,
                        ),
                        name      = filename,
                    )
                    if (not upscaled) or opt.save_original:
                        # only append to results if we didn't overwrite an earlier output
                        results.append([path, formatted_dream_prompt])
                last_results.append([path, seed])

            catch_ctrl_c = infile is None # if running interactively, we catch keyboard interrupts
            gen.prompt2image(
                image_callback=image_writer,
                catch_interrupts=catch_ctrl_c,
                **vars(opt)
            )

            if opt.grid and len(grid_images) > 0:
                grid_img   = make_grid(list(grid_images.values()))
                grid_seeds = list(grid_images.keys())
                first_seed = last_results[0][1]
                filename   = f'{prefix}.{first_seed}.png'
                formatted_dream_prompt  = opt.dream_prompt_str(seed=first_seed,grid=True,iterations=len(grid_images))
                formatted_dream_prompt += f' # {grid_seeds}'
                metadata = metadata_dumps(
                    opt,
                    seeds      = grid_seeds,
                    weights    = gen.weights,
                    model_hash = gen.model_hash
                    )
                path = file_writer.save_image_and_prompt_to_png(
                    image        = grid_img,
                    dream_prompt = formatted_dream_prompt,
                    metadata     = metadata,
                    name         = filename
                )
                results = [[path, formatted_dream_prompt]]

        except AssertionError as e:
            print(e)
            continue

        except OSError as e:
            print(e)
            continue

        print('Outputs:')
        log_path = os.path.join(current_outdir, 'dream_log.txt')
        write_log_message(results, log_path)
        print()

    print('goodbye!')


def get_next_command(infile=None) -> str:  # command string
    if infile is None:
        command = input('dream> ')
    else:
        command = infile.readline()
        if not command:
            raise EOFError
        else:
            command = command.strip()
        if len(command)>0:
            print(f'#{command}')
    return command

def dream_server_loop(gen, host, port, outdir):
    print('\n* --web was specified, starting web server...')
    # Change working directory to the stable-diffusion directory
    os.chdir(
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    )

    # Start server
    DreamServer.model  = gen # misnomer in DreamServer - this is not the model you are looking for
    DreamServer.outdir = outdir
    dream_server = ThreadingDreamServer((host, port))
    print(">> Started Stable Diffusion dream server!")
    if host == '0.0.0.0':
        print(
            f"Point your browser at http://localhost:{port} or use the host's DNS name or IP address.")
    else:
        print(">> Default host address now 127.0.0.1 (localhost). Use --host 0.0.0.0 to bind any address.")
        print(f">> Point your browser at http://{host}:{port}.")

    try:
        dream_server.serve_forever()
    except KeyboardInterrupt:
        pass

    dream_server.server_close()


def write_log_message(results, log_path):
    """logs the name of the output image, prompt, and prompt args to the terminal and log file"""
    global output_cntr
    log_lines = [f'{path}: {prompt}\n' for path, prompt in results]
    for l in log_lines:
        output_cntr += 1
        print(f'[{output_cntr}] {l}',end='')


    with open(log_path, 'a', encoding='utf-8') as file:
        file.writelines(log_lines)

if __name__ == '__main__':
    main()
